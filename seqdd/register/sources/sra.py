import logging
from os import listdir, makedirs, path, remove
import platform
from shutil import rmtree, move
import subprocess
from threading import Lock
import time

from seqdd.utils.download import check_binary
from seqdd.utils.scheduler import Job, CmdLineJob, FunctionJob


naming = {
    'name': 'SRA',
    'key': 'sra',
    'classname': 'SRA'
}


class SRA:
    """
    The SRA class represents a data downloader for the Sequence Read Archive (SRA) database.

    Attributes:
        tmpdir (str): The temporary directory path.
        bindir (str): The binary directory path.
        logger: The logger object for logging messages.
        binaries (dict): A dictionary containing the paths to the SRA toolkit binaries.
        mutex: A lock object for thread synchronization.
        min_delay (float): The minimum delay between SRA queries in seconds.
        last_sra_query (float): The timestamp of the last SRA query.

    """
    
    def __init__(self, tmpdir: str, bindir: str, logger: logging.Logger) -> None:
        """
        Initialize the SRA downloader object.

        Args:
            tmpdir (str): The temporary directory path.
            bindir (str): The binary directory path.
            logger: The logger object.
        """
        self.tmpdir = tmpdir
        self.bindir = bindir
        self.logger = logger
        self.binaries = self.download_sra_toolkit()
        
        self.mutex = Lock()
        self.min_delay = 0.5
        self.last_sra_query = 0

    def is_ready(self) -> bool:
        """
        Checks if the SRA toolkit binaries are ready for use.

        Returns:
            bool: True if the binaries are ready, False otherwise.
        """
        return self.binaries is not None
    
    def sra_delay_ready(self) -> bool:
        """
        Checks if the minimum delay between SRA queries has passed.

        Returns:
            bool: True if the minimum delay has passed, False otherwise.
        """
        # Minimal delay between SRA queries (0.5s)
        locked = self.mutex.acquire(blocking=False)
        ready = False
        if locked:
            ready = time.time() - self.last_sra_query > self.min_delay
            if ready:
                self.last_sra_query = time.time()
            self.mutex.release()
        return ready
    
    def filter_valid_accessions(self, accessions: list[str]) -> list[str]:
        """
        Filters the given list of SRA accessions and returns only the valid ones.

        Args:
            accessions (list): A list of SRA accessions.

        Returns:
            list: A list of valid SRA accessions.
        """
        # print('TODO: Validate sra accessions...')
        return accessions

    
    def jobs_from_accessions(self, accessions: list[str], datadir: str) -> list[Job]:
        """
        Generates a list of jobs for downloading and processing SRA datasets.

        Args:
            accessions (list): A list of SRA accessions.
            datadir (str): The output directory path.

        Returns:
            list: A list of jobs for downloading and processing SRA datasets.
        """
        jobs = []

        # Each dataset download is independent
        for acc in accessions:
            # Do not download an already downloaded dataset
            if path.exists(path.join(datadir, acc)):
                self.logger.info(f'{acc} already downloaded. Skipping...')
                continue

            # Create a job name based on the accession
            job_name = f'sra_{acc}'
            
            # Create a tmp directory for the accession. Remove the previous one if it exists
            acc_dir = path.join(self.tmpdir, acc)
            if path.exists(acc_dir):
                rmtree(acc_dir)
            makedirs(acc_dir)


            # Fasterq-dump
            prefetch_job = None
            fasterq_dump_jobs = []
            if acc.startswith('SRR'):
                # Prefetch data
                cmd = f'{self.binaries["prefetch"]} --max-size u --output-directory {self.tmpdir} {acc}'
                prefetch_job = CmdLineJob(cmd, can_start=self.sra_delay_ready, name=f'{job_name}_prefetch')
                # Download SRR accession
                fasterq_dump_jobs = self.jobs_from_SRR(acc_dir, job_name)
            elif acc.startswith('SRX') or acc.startswith('SRP'):
                # Prefetch data
                cmd = f'{self.binaries["prefetch"]} --max-size u --output-directory {acc_dir} {acc}'
                prefetch_job = CmdLineJob(cmd, can_start=self.sra_delay_ready, name=f'{job_name}_prefetch')
                fasterq_dump_jobs = self.jobs_from_SRXP(acc_dir, job_name)

            # define parents
            for job in fasterq_dump_jobs:
                job.parents.append(prefetch_job)

            # Move to datadir and clean tmpdir
            clean_job = FunctionJob(self.move_and_clean,
                                    func_args=(acc_dir, datadir),
                                    parents=fasterq_dump_jobs,
                                    name=f'{job_name}_clean')

            # Set the jobs
            jobs.append(prefetch_job)
            jobs.extend(fasterq_dump_jobs)
            jobs.append(clean_job)

        return jobs
    

    def move_and_clean(self, accession_dir: str, outdir: str) -> None:
        """
        Moves the downloaded files from the accession directory to the output directory and cleans up the temporary directory.

        Args:
            accession_dir (str): The directory path containing the downloaded files.
            outdir (str): The output directory path.
        """
        # Get the accession name
        accession = path.basename(accession_dir)
        outdir = path.join(outdir, accession)
        makedirs(outdir, exist_ok=True)

        # Enumerate all the files from the accession directory
        for node in listdir(accession_dir):
            nodepath = path.join(accession_dir, node)
            # Move SRR accession files
            if path.isfile(nodepath) and node.endswith('.gz'):
                move(path.join(accession_dir, node), path.join(outdir, node))

        # Clean the directory
        rmtree(accession_dir)


    # ---- SRA specific jobs ----

    def jobs_from_SRR(self, accession_dir: str, job_name: str) -> list[CmdLineJob, CmdLineJob]:
        # Split files
        cmd = f'{self.binaries["fasterq-dump"]} --split-3 --skip-technical --outdir {accession_dir} {accession_dir}'
        fasterqdump_job = CmdLineJob(cmd, can_start=self.sra_delay_ready, name=f'{job_name}_fasterqdump')
        
        # Compress files
        cmd = f'gzip {path.join(accession_dir, "*.fastq")}'
        compress_job = CmdLineJob(cmd, parents=[fasterqdump_job], name=f'{job_name}_compress')

        return [fasterqdump_job, compress_job]
    

    def jobs_from_SRXP(self, acc_dir, job_name):
        SRXP_subjob = FunctionJob(
            self.run_fasterqdump_from_SRXP,
            func_args=(acc_dir,),
            name=f'{job_name}_fasterqdump+gzip'
        )

        return [SRXP_subjob]
    

    def run_fasterqdump_from_SRXP(self, SRXP_directory: str) -> None:
        # TODO: Move log files to the log directory. Can be done after yielding.
        SRX_name = path.basename(SRXP_directory)

        for subdirectory_name in listdir(SRXP_directory):
            subdirectory = path.join(SRXP_directory, subdirectory_name)
            # Verify that is a run subdirectory
            if (not path.isdir(subdirectory)) or (not subdirectory_name.startswith('SRR')):
                continue
            
            res = None
            log_file = path.join(self.tmpdir, f'{SRX_name}_{subdirectory_name}_fasterq-dump.log')
            
            # Split the sra files into fastq files
            cmd = f'{self.binaries["fasterq-dump"]} --split-3 --skip-technical --outdir {subdirectory} {subdirectory}'
            with open(log_file, 'w') as log:
                res = subprocess.run(cmd.split(), stdout=log, stderr=log)
            if res.returncode != 0:
                self.logger.error(f'Error while running fasterq-dump on {subdirectory}')
                raise Exception(f'Error while running fasterq-dump on {subdirectory}')
            
            res = None
            log_file = path.join(self.tmpdir, f'{SRX_name}_{subdirectory_name}_gzip.log')
            fp = open(log_file, 'w')
            fp.close()


            # Compress fastq files
            for filename in listdir(subdirectory):
                if not filename.endswith('.fastq'):
                    continue
                cmd = f'gzip {path.join(subdirectory, filename)}'
                with open(log_file, 'a') as log:
                    res = subprocess.run(cmd.split(), stdout=log, stderr=log)
                if res.returncode != 0:
                    self.logger.error(f'Error while compressing fastq files in {subdirectory}')
                    raise Exception(f'Error while compressing fastq files in {subdirectory}')
            
                gzip_filename = f'{filename}.gz'
                # Move the compressed files to the SRXP directory
                move(path.join(subdirectory, gzip_filename), path.join(SRXP_directory, gzip_filename))

            # Remove the subdirectory
            rmtree(subdirectory)
    

    # ---- Toolkit preparation ----
    
    def download_sra_toolkit(self) -> dict[str, str]:
        """
        Downloads and installs the SRA toolkit if necessary, and returns the paths to the SRA toolkit binaries.

        Returns:
            dict: A dictionary containing the paths to the SRA toolkit binaries.
        """
        # Check if the system has the ncbi datasets cli
        prefetch_installed = check_binary('prefetch')
        fasterqdump_installed = check_binary('fasterq-dump')
        if prefetch_installed and fasterqdump_installed:
            return {
                'prefetch': 'prefetch',
                'fasterq-dump': 'fasterq-dump'
            }
        
        # Check if the software is locally installed
        prefetch_local_bin = path.abspath(path.join(self.bindir, 'prefetch'))
        fasterqdump_local_bin = path.abspath(path.join(self.bindir, 'fasterq-dump'))
        prefetch_installed = check_binary(prefetch_local_bin)
        fasterqdump_installed = check_binary(fasterqdump_local_bin)
        
        if prefetch_installed and fasterqdump_installed:
            return {
                'prefetch': f'{prefetch_local_bin}',
                'fasterq-dump': f'{fasterqdump_local_bin}'
            }
        
        # Install the software
        return self.install_sratoolkit()

    def install_sratoolkit(self, version='3.1.1') -> dict[str, str]|None:
        """
        Downloads and installs the SRA toolkit with the specified version.

        Args:
            version (str): The version of the SRA toolkit to install. Default is '3.1.1'.

        Returns:
            dict: A dictionary containing the paths to the SRA toolkit binaries, or None if installation failed.
        """
        download_link = ''
        dirname = ''
        supported = True

        # Local install
        system = platform.system()
        if system == 'Linux':
            download_link = f'https://ftp-trace.ncbi.nlm.nih.gov/sra/sdk/{version}/sratoolkit.{version}-ubuntu64.tar.gz'
            dirname = f'sratoolkit.{version}-ubuntu64'
            tarname = f'{dirname}.tar.gz'
        else:
            supported = False

        # Message to potential system extensions
        if not supported:
            self.logger.critical(f'sratoolkit auto-install is not yet supported on your system. SRA downloader has not been installed... Also maybe we can include your system in the auto-installer. Please submit an issue on github with the following values:\nsystem={system}\tplateform={platform.machine()}')
            return None

        # Download sra toolkit
        tmp_dir = path.abspath(self.tmpdir)
        makedirs(tmp_dir, exist_ok=True)
        tarpath = path.join(tmp_dir, tarname)

        self.logger.info('Download the sratoolkit binnaries...')

        cmd = f'curl -o {tarpath} {download_link}'
        ret = subprocess.run(cmd.split(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        if ret.returncode != 0:
            self.logger.error('Impossible to automatically download sratoolkit. SRA downloader has not been installed...')
            return None

        # Uncompress the archive
        cmd = f'tar -xzf {tarpath} -C {self.bindir}'
        ret = subprocess.run(cmd.split(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        remove(tarpath)

        if ret.returncode != 0:
            self.logger.error('Impossible to expand the sratoolkit tar.gz on your system.')
            return None

        # Create links to the bins
        prefetch_bin = path.abspath(path.join(self.bindir, dirname, 'bin', 'prefetch'))
        prefetch_ln = path.abspath(path.join(self.bindir, 'prefetch'))
        cmd = f'ln -s {prefetch_bin} {prefetch_ln}'
        ret = subprocess.run(cmd.split())
        if ret.returncode != 0:
            self.logger.error(f'Impossible to create symbolic link {prefetch_ln}. SRA downloader has not been installed...')
            return None

        fasterqdump_bin = path.abspath(path.join(self.bindir, dirname, 'bin', 'fasterq-dump'))
        fasterqdump_ln = path.abspath(path.join(self.bindir, 'fasterq-dump'))
        cmd = f'ln -s {fasterqdump_bin} {fasterqdump_ln}'
        ret = subprocess.run(cmd.split())
        if ret.returncode != 0:
            self.logger.error(f'Impossible to create symbolic link {fasterqdump_ln}. SRA downloader has not been installed...')
            return None
        
        self.logger.info(f'SRA downloader binaries installed at {self.bindir}')

        return {
            'prefetch' : prefetch_ln,
            'fasterq-dump' : fasterqdump_ln
        }
        

# --- Cmds ---
# ./sratoolkit.3.1.1-ubuntu64/bin/prefetch --max-size u --output-directory outtest SRR000001
# ./sratoolkit.3.1.1-ubuntu64/bin/fasterq-dump --split-3 --skip-technical --outdir outtest outtest/SRR000001


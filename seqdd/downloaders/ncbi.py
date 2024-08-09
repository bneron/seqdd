from os import listdir, makedirs, path, remove, rename
import platform
from shutil import rmtree
import subprocess
from sys import stderr
from threading import Lock
import time

from seqdd.utils.scheduler import CmdLineJob, FunctionJob
from seqdd.downloaders.download import check_binary
import json


class NCBI:
    ncbi_joib_id = 0

    def __init__(self, tmpdir, bindir):
        self.tmp_dir = tmpdir
        self.bin_dir = bindir
        self.mutex = Lock()

        self.bin = self.get_download_software()
        self.last_ncbi_query = 0

    def is_ready(self):
        return self.bin is not None
    
    def ncbi_delay_ready(self):
        # Minimal delay between ncbi queries (1s)
        min_delay = 1
        locked = self.mutex.acquire(blocking=False)
        ready = False
        if locked:
            # 5s since the last query ?
            ready = time.time() - self.last_ncbi_query > min_delay
            if ready:
                self.last_ncbi_query = time.time()
            self.mutex.release()
        return ready
    
    def jobs_from_accessions(self, accessions, dest_dir):

        accessions = list(accessions)
        all_jobs = []

        # Download accessions by batch of 5
        for idx in range(0, len(accessions), 5):
            # Create a temporary directory for the current job
            tmp_dir = path.join(self.tmp_dir, f'ncbi_{NCBI.ncbi_joib_id}')
            makedirs(tmp_dir, exist_ok=True)
            # Job name
            job_name = f'ncbi_job_{NCBI.ncbi_joib_id}'
            NCBI.ncbi_joib_id += 1

            # Take the right slice of 5 accessions
            acc_slice = accessions[idx:idx+5]

            # Download dehydrated job
            download_file = path.join(tmp_dir, f'{job_name}.zip')
            download_job = CmdLineJob(f"{self.bin} download genome accession --dehydrated --filename {download_file} {' '.join(acc_slice)}", can_start=self.ncbi_delay_ready)
            
            # Unzip Job
            unzip_dir = path.join(tmp_dir, job_name)
            unzip_job = CmdLineJob(f"unzip {download_file} -d {unzip_dir}", parents=[download_job])

            # Data download
            rehydrate_job = CmdLineJob(f"{self.bin} rehydrate --gzip --no-progressbar --directory {unzip_dir}", parents=[unzip_job], can_start=self.ncbi_delay_ready)

            # Data reorganization
            reorg_job = FunctionJob(self.clean, func_args=(unzip_dir, dest_dir, tmp_dir), parents=[rehydrate_job])

            all_jobs.extend([download_job, unzip_job, rehydrate_job, reorg_job])

        return all_jobs


    def clean(self, unzip_dir, dest_dir, tmp_dir):
        # Remove subdirectories while moving their content
        data_dir = path.join(unzip_dir, "ncbi_dataset", "data")

        # Enumerated the downloaded files
        for subname in listdir(data_dir):
            subpath = path.join(data_dir, subname)
            # Looks only for datasets
            if path.isdir(subpath):
                # Looks only for datafiles
                for file in listdir(subpath):
                    if file.endswith(".gz"):
                        filepath = path.join(subpath, file)
                        # Move the data to its final destination
                        rename(filepath, path.join(dest_dir, file))

        # Clean the download directory
        rmtree(tmp_dir)

    def filter_valid_accessions(self, accessions):
        accessions_list = list(accessions)
        valid_accessions = set()

        for idx in range(0, len(accessions), 10):
            # Accessions slice to validate
            accessions_slice = accessions_list[idx:idx+10]

            # Create a temporary directory for the current validation
            tmp_path = path.join(self.tmp_dir, f'ncbi_valid_{idx}')
            makedirs(tmp_path)
            archive_path = path.join(tmp_path, 'accessions.zip')

            # Download the accessions info
            cmd = f'{self.bin} download genome accession {" ".join(accessions_slice)} --include none --filename {archive_path}'
            ret = subprocess.run(cmd.split())

            # Check download status
            if ret.returncode != 0:
                print(f'Datasets software error while downloading the accessions info: {ret.stderr}', file=stderr)
                print(f'Skipping the validation of the accessions: {accessions_slice}', file=stderr)
                continue

            # Unzip the accessions info
            unzip_path = path.join(tmp_path, 'accessions')
            cmd = f'unzip {archive_path} -d {unzip_path}'
            ret = subprocess.run(cmd.split())

            # Check unzip status
            if ret.returncode != 0:
                print(f'Impossible to unzip the accessions info: {archive_path}', file=stderr)
                print(f'Skipping the validation of the accessions: {accessions_slice}', file=stderr)
                continue

            # Check the accessions
            with open(path.join(unzip_path, 'ncbi_dataset', 'data', 'assembly_data_report.jsonl')) as fr:
                for line in fr:
                    # parse the json from the line
                    data = json.loads(line)
                    line_acc = data['accession']
                    if line_acc in accessions_slice:
                        valid_accessions.add(line_acc)

            # Clean the temporary directory
            rmtree(tmp_path)
            
        # Print the list of invalid accessions
        invalid_accessions = accessions - valid_accessions
        if len(invalid_accessions) > 0:
            print(f'The following accessions are invalid: {", ".join(list(invalid_accessions))}', file=stderr)
            print('Those accessions will be ignored.', file=stderr)

        return valid_accessions
    

    def get_download_software(self):
        # Check if the system has the ncbi datasets cli
        system_installed = check_binary('datasets')
        if system_installed:
            return 'datasets'
        
        # Check if the software is locally installed
        local_bin = path.abspath(path.join(self.bin_dir, 'datasets'))
        locally_installed = check_binary(local_bin)
        if locally_installed:
            return f'{local_bin}'
        
        # Install the software
        return self.install_datasets_software()

    def install_datasets_software(self):
        download_link = ''
        supported = True

        system = platform.system()
        if system == 'Linux':
            cpu_type = platform.machine()
            if cpu_type in ['i386', 'i686', 'x86_64', 'x86', 'AMD64']:
                download_link = 'https://ftp.ncbi.nlm.nih.gov/pub/datasets/command-line/v2/linux-amd64/datasets'
            elif cpu_type in ['aarch64_be', 'aarch64', 'armv8b', 'armv8l', 'arm']:
                download_link = 'https://ftp.ncbi.nlm.nih.gov/pub/datasets/command-line/v2/linux-arm64/datasets'
            else:
                supported = False
        else:
            supported = False

        # Message to potential system extensions
        if not supported:
            print('ncbi datasets auto-install is not yet supported on your system. Plese install ncbi datasets cli by yourself. Also maybe we can include your system in the auto-installer. Please submit an issue on github with the following values:', file=stderr)
            print(f'system={system}\tplateform={platform.machine()}', file=stderr)
            return None

        # Download datasets
        print('Download the ncbi datasets cli binnary...')
        
        # Prepare the bin directory
        download_dir = path.abspath(self.bin_dir)
        makedirs(download_dir, exist_ok=True)
        
        # Download...
        cmd = f'curl -o {path.join(download_dir, "datasets")} {download_link}'
        ret = subprocess.run(cmd.split())

        if ret.returncode == 0:
            binpath = path.join(download_dir, 'datasets')

            ret = subprocess.run(f'chmod +x {binpath}'.split())
            if ret.returncode == 0:
                # move the binary to the bin directory
                final_path = path.abspath(path.join(self.bin_dir, 'datasets'))
                rename(binpath, final_path) 

                return f'{final_path}'
            else:
                print(f'Impossible to change the exec rights for {binpath}. Automatic download of ncbi datasets cli is aborted. Please install it by yourself.', file=stderr)
        else:
            print('Impossible to automatically download ncbi datasets cli. Please install it by yourself.', file=stderr)

        return None
    
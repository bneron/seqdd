from os import path, makedirs
from shutil import rmtree
from sys import stderr
import subprocess
import time

from seqdd.utils.scheduler import JobManager
from ..register.reg_manager import Register
from ..register.src_manager import SourceManager

# -------------------- Global download manager --------------------

class DownloadManager:

    def __init__(self, register: Register, src_manager: SourceManager,
                 logger: str, bindir: str ='bin', tmpdir: str ='/tmp'):
        self.register = register

        self.bindir = bindir
        self.tmpdir = tmpdir
        self.logger = logger

        self.src_manager = src_manager


    def download_to(self, datadir, logdir, max_process=8):
        """
        Downloads datasets from different sources to the specified data directory.

        Args:
            datadir (str): The path to the data directory where the datasets will be downloaded.
            logdir (str): The path to the log directory where the log files will be stored.
            max_process (int, optional): The maximum number of processes to use for downloading. Defaults to 8.

        """
        # Creates the tmp and data directory if it doesn't exist
        makedirs(datadir, exist_ok=True)
        if logdir is not None and path.exists(logdir):
            rmtree(logdir)
        makedirs(logdir)

        # Create a dictionary to store the jobs for each source
        jobs = {source: [] for source in self.register.acc_by_src}

        # Create the jobs for each source
        for source in self.register.acc_by_src:
            reg = self.register.acc_by_src[source]
            manipulator = self.src_manager.get(source)

            if len(reg) > 0:
                if manipulator.is_ready():
                    jobs[source] = manipulator.jobs_from_accessions(reg, datadir)
                    self.logger.info(f'{len(reg)} datasets from {source} will be downloaded.')
                else:
                    self.logger.warning(f'{source} datasets cannot be downloaded because the downloader is not ready. Skipping {len(reg)} datasets.', file=stderr)

        # Create a JobManager instance
        manager = JobManager(max_process=max_process, log_folder=logdir, logger=self.logger)
        manager.start()

        # Add jobs to the JobManager in an interleaved way.
        # Doing this will allow the JobManager to start jobs from different sources in parallel.
        idxs = {source: 0 for source in jobs}
        total_jobs = sum([len(jobs[source]) for source in jobs])
        while total_jobs > 0:
            for source in jobs:
                if idxs[source] < len(jobs[source]):
                    manager.add_job(jobs[source][idxs[source]])
                    idxs[source] += 1
                    total_jobs -= 1

        # Wait for all jobs to complete
        while manager.remaining_jobs() > 0:
            time.sleep(1)

        # Stop and join the JobManager
        manager.stop()
        manager.join()


# -------------------- Utils downloads --------------------


def check_binary(path_to_bin):
    """ Check if the binary is present and executable
    :param: path_to_bin Path to the binary

    :return: True if the binary is present and executable. False otherwise.
    """
    try:
        cmd = f'{path_to_bin} --version'
        ret = subprocess.run(cmd.split(' '), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return ret.returncode == 0
    except FileNotFoundError:
        return False
    



#!/usr/bin/env python

"""

This script is designed to neatly handle large amounts of MinION data.
At first MinION fast5 file structure was quite cool and a novel experience,
however the yield from a set MinION run has sky-rocketed beyond even what Clive Brown
expected.
We now have the predicament that one read for each fasta file is simply too much
for a computer/server to handle.

ONT have taken the step in MinKNOW version 1.4+ to reduce some problems by restricting
the number of files in
each folder to 4000. This is done by creating sub-folders in the 'reads' directory:
0, 1, 2, as needed.

However this comes with a couple of bugs and a couple more issues.
Any scripts that relied on all reads being in one folder now have to implement a
recursive stage, and the number of files isn't strictly 4000.
Why? Because 'mux-reads' don't seem to count
(so folder 0 will often have around 6000-7000 reads), and
if a run is restarted then you will end up with at least 8000 reads in each folder.

Scratch that, MinKNOW 1.6+ now generates a separate folder for each sequencing run.
This has a minor problem in that mux scans and sequencing runs are considered separate runs.

This script is designed to:
1. Detect any 'completed' folders, those that have 4000 reads in them.
   1a. Rename this folder specific to this run so it won't be accidentally overwritten.
2. Tar up, check integrity and then md5sum this folder.
3. Rsync the tar.gz file over to the server, and remove the source file to save space on the computer.

We use the ps -ef command to search for an active MinKNOW session.
"""

# Import necessary modules,
import os  # list directories and path checking
import sys  # For stopping in the event of errors.
import subprocess  # Running rsync and tar functions.
import argparse  # Allow users to set commandline arguments and show help
import getpass  # Prompts user for password, just a one off to runt he script.
from pexpect import pxssh, spawn  # Connecting via ssh to make sure that the parent of the
                                  #  destination folder is there.
import time  # For snoozing and for adding time of generation in csv output.
import pandas as pd  # Create data frame of list of files with attributes for each.

# Set global variables that aren't actually global,
# Just easier than piping them into everything.
READS_DIR = ""
SERVER_NAME = ""
SERVER_USERNAME = ""
PASSWORD = ""
DEST_DIRECTORY = ""
TIMEOUT = 1800
CSV_DIR = ""
RSYNC_SUBPROCESS = None
CHECK_SUMS_FILE = ""
PARENT_DIRECTORY = ""
MINKNOW_RUNNING = True
TRANSFER_LOCK_FILE = "TRANSFERRING"
SAMPLE_NAME = ""
RUNS = []
MUX_PROCESSING_TIME = 600 # seconds


class Run:
    def __init__(self, name, flowcell, random, mux, sample_id):
        self.name = name
        self.flowcell = flowcell
        self.random = random
        self.mux = mux
        self.sample_id = sample_id
        date, time = name.split("_")[0:1]
        self.start_time = time.strptime(date + time, "%Y%m%d%H%M")
        self.dir = os.path.join(READS_DIR, name)
        self.fast5_dir = os.path.join(READS_DIR, name, 'fast5')
        self.csv_dir = os.path.join(READS_DIR, name, 'csv')


def main():
    # Get argument list, password for server and set directories.
    args = get_arguments()
    set_global_variables(args)
    check_directories()

    # While loop to continue tarring up folders
    create_transferring_lock_file()  # Indicator for down-the-line base
                                     # callers that more data is coming!
    # Get rsync up and running
    run_rsync_command()

    while MINKNOW_RUNNING:
        # Commence transfer of fast5 files.
        set_runs()
        for run in RUNS:
            transfer_fast5_files(run)

    # Now we need to tar up the last folder.
    # create last folder.
    # Send across csv(s) and md5sum file.

    tar_up_last_folder()
    copy_across_md5sum()
    copy_across_csv_files()

    # Remove the lock file from the server.
    remove_transferring_lock_file()


def set_runs():
    global RUNS
    runs = [run for run in os.listdir(READS_DIR)
            if os.path.isdir(os.path.join(READS_DIR, run))
            and run.endswith(SAMPLE_NAME)]

    initialised_runs = []
    for initalised_run in RUNS:
        initialised_runs.append(initalised_run.name)

    for run in runs:
        if run in initialised_runs:
            continue

        flowcell, random, mux = get_run_details(run)

        RUNS.append(Run(run, flowcell, random, mux, SAMPLE_NAME))
    return runs


def get_run_details(run):
    # Get list of files in the first directory we come across.
    subfolders = sorted([folder for folder in os.listdir(run.fast5_dir)
                         if os.path.isdir(run.fast5_dir, folder)
                         and is_int(folder)])
    # Get the first subfolder we see.
    folder = subfolders[0]
    subfolder_path = os.path.join(run.fast5_dir, folder)
    subfolder_df = pd.DataFrame(columns=["filename", "flowcell", "random"])
    fast5_files = [filename for filename in os.listdir(subfolder_path)
                   if filename.endswith(".fast5")]
    # We need to know where 'sequencing_run' sits in the name of the file.
    # In order to pick out the flowcell ID.
    # alexis_MacBookPro_20170518_FNFAF18353_MN19582_sequencing_run_...
    # PRAWN_P28_R9p4_11874_ch162_read134_strand.fast5
    # Or it's mux scan
    sequencing_run_index = fast5_files[0].index("sequencing_run")
    mux = False
    if sequencing_run_index == -1:
        mux = True
        sequencing_run_index = fast5_files[0].index("mux_scan")

    subfolder_df["filename"] = fast5_files
    subfolder_df['flowcell'] = [fast5_file.split('_')[sequencing_run_index - 2]
                                for fast5_file in fast5_files]
    subfolder_df['random'] = [fast5_file.split('_')[-4]
                              for fast5_file in fast5_files]

    flowcell = subfolder_df['flowcell'].unique().tolist()[0]
    random = subfolder_df['random'].unique().tolist()[0]

    # Check that all of these are the same
    if not len(subfolder_df['flowcell'].unique().tolist()) == 1 \
        or not len(subfolder_df['random'].unique().tolist()) == 1:
        sys.exit("Houston, it appears that one of these files is not like the others.")

    return flowcell, random, mux


def is_int(folder):
    try:
        int(folder)
    except ValueError:
        return False
    return True


def create_transferring_lock_file():
    s = pxssh.pxssh()
    print(PASSWORD)
    if not s.login(SERVER_NAME, SERVER_USERNAME, PASSWORD):
        print("SSH failed on login")
    else:
        print("SSH passed")

    # Command to check if folder is there.
    s.sendline('cd %s && touch %s' % (DEST_DIRECTORY, TRANSFER_LOCK_FILE))
    s.prompt()  # match the prompt
    output = s.before  # Gets the `output of the send line command
    s.logout()  # Logout


def remove_transferring_lock_file():
    s = pxssh.pxssh()

    if not s.login(SERVER_NAME, SERVER_USERNAME, PASSWORD):
        print("SSH failed on login")
    else:
        print("SSH passed")

    # Command to check if folder is there.
    s.sendline('cd %s && rm %s' % (DEST_DIRECTORY, TRANSFER_LOCK_FILE))
    s.prompt()  # match the prompt
    output = s.before  # Gets the `output of the send line command
    s.logout()  # Logout


def transfer_fast5_files(run):
    global MINKNOW_RUNNING
    # Get list of sub-directories
    subdirs = get_subdirs(run)
    print(subdirs)

    # Check if MinKNOW is still running
    if not is_minknow_still_running():
        MINKNOW_RUNNING = False

    new_folders = False
    # For any new folders.
    for subdir in subdirs:
        subdir_as_standard_int = standardise_int_length(os.path.basename(os.path.normpath(subdir)))
        # Is folder finished?
        folder_status = check_folder_status(subdir, run=run)
        if folder_status == "still writing":
            continue
        new_folders = True
        # Tar up folder(s)
        tar_folders(subdir_as_standard_int)

        if RSYNC_SUBPROCESS.poll() is not None:
            stdout, stderr = RSYNC_SUBPROCESS.communicate()
            print(stdout, stderr)
            # Sync up with the rest of the team
            run_rsync_command()
        copy_across_md5sum()  # Update the md5sum
        copy_across_csv_files()  # Update the csv file list.

    # Let's have a rest if no new folders have been created recently.
    if not new_folders:
        have_a_break()


def get_arguments():
    parser = argparse.ArgumentParser(
        description="The transfer_fast5_to_server transfers MiNION data from a " +
                    "laptop in realtime. The process will finish when the script " +
                    "believes MinKNOW is no longer running.")
    parser.add_argument("--reads_dir", type=str, required=True,
                        help="/path/to/reads, " +
                             "should have a bunch of subfolders from 0 to N")
    parser.add_argument("--server_name", type=str, required=True,
                        help="If you were to ssh username@server, " +
                             "please type in the server bit.")
    parser.add_argument("--user_name", type=str, required=True,
                        help="If you were to ssh username@server, " +
                             "please type in the username bit")
    parser.add_argument("--dest_directory", type=str, required=True,
                        help="Where abouts on the server do you wish to place these files?")
    parser.add_argument("--sample_name", type=str, required=True,
                        help="Sample name that you typed into MinKNOW.")
    return parser.parse_args()


def set_global_variables(args):
    global READS_DIR, SERVER_NAME, SERVER_USERNAME, PASSWORD, \
           DEST_DIRECTORY, TIMEOUT, PARENT_DIRECTORY, FLOWCELL
    READS_DIR = args.reads_dir
    SERVER_NAME = args.server_name
    SERVER_USERNAME = args.user_name
    PASSWORD = get_password()
    DEST_DIRECTORY = args.dest_directory
    PARENT_DIRECTORY = os.path.abspath(os.path.join(READS_DIR, os.pardir))


def get_password():
    return getpass.getpass('password: ')


def is_minknow_still_running():
    # For linux systems we can use the ps -ef command to see what processes are running.
    # ps stands for process status, -e for everyone, -f for full.
    # We can check for the minknow python script. File.
    # If this has stopped, there will be no more reads produced and we can tar up the
    # last folder and perform one last rsync command.

    is_running = False  # Now to disprove this.

    psef_command = "ps -ef | grep MinKNOW"
    psef_proc = subprocess.Popen(psef_command, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, shell=True)
    stdout, stderr = psef_proc.communicate()
    # Split stdout by line, should be a bunch of MinKNOW commands running
    for line in stdout.split("\n"):
        if line.endswith(".py") and "python" in line:
            is_running = True  # The nc python script is presently active.

    # Now return what we found.
    return is_running


def tar_up_last_folder():
    for subdir in get_subdirs():
        subdir_as_standard_int = standardise_int_length(os.path.basename(os.path.normpath(subdir)))
        # Don't worry about counting the number of files, we're finished.
        check_folder_status(subdir, full=False)
        tar_folders(subdir_as_standard_int)
    # Perform final rsync command if need be:
    if RSYNC_SUBPROCESS.poll() is not None:
        stdout, stderr = RSYNC_SUBPROCESS.communicate()
        print(stdout, stderr)
        # Sync up with the rest of the team
        run_rsync_command()


def check_directories():
    global READS_DIR, CSV_DIR, CHECK_SUMS_FILE
    # Check if reads directory exists
    if not os.path.isdir(READS_DIR):
        sys.exit("Error, reads directory, %s, does not exist" % READS_DIR)
    READS_DIR = os.path.abspath(READS_DIR) + "/"
    # We will now continue working from the reads directory.
    os.chdir(READS_DIR)

    # Create CSV directory if it does not exist
    CSV_DIR = READS_DIR + "csv/"
    if not os.path.isdir(CSV_DIR):
        os.mkdir(CSV_DIR)
    CHECK_SUMS_FILE = READS_DIR + "checksums.md5"

    # Check if server is active using the ping command.
    ping_command = subprocess.Popen("ping -c 1 %s" % SERVER_NAME, shell=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    ping_command.wait()
    out, error = ping_command.communicate()
    print(out, error)

    # Check if folder on server is present.
    # Log into server, then check for folder.
    dest_parent = '/'.join(DEST_DIRECTORY.split("/")[:-2])
    
    s = pxssh.pxssh()  
    
    print(repr(PASSWORD)) 
    if not s.login(SERVER_NAME, SERVER_USERNAME, PASSWORD):
        print("SSH failed on login")
    else:
        print("SSH passed")

    # Command to check if folder is there.
    s.sendline('if [ -d %s ]; then echo "PRESENT"; fi' % dest_parent)
    s.prompt()  # match the prompt
    output = s.before  # Gets the `output of the send line command
    print(dest_parent, output)
    if not output.rstrip().split('\n')[-1] == "PRESENT":
        # Parent folder is not present. Exit.
        sys.exit("Error, parent directory of %s does not exist" % DEST_DIRECTORY)

    # Otherwise create the DEST_DIRECTORY
    s.sendline('if [ ! -d %s ]; then mkdir %s; fi' % (DEST_DIRECTORY, DEST_DIRECTORY))
    s.prompt()
    output = s.before
    print(output)


def copy_across_md5sum():
    # Use the scp command to copy across the md5sum file into the
    # destination directory on the server
    scp_command = "sshpass -p %s scp %s %s@%s:%s" % (
                                                     PASSWORD,
                                                     CHECK_SUMS_FILE,
                                                     SERVER_USERNAME,
                                                     SERVER_NAME,
                                                     DEST_DIRECTORY
                                                     )
    scp_proc = subprocess.Popen(scp_command, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, shell=True)
    stdout, stderr = scp_proc.communicate()
    print("Output of scp command", stdout, stderr)


def copy_across_csv_files():
    # Use the scp command to copy across the csv files into the destination directory on the server.
    scp_command = "sshpass -p %s scp -r %s %s@%s:%s" % (
                                                        PASSWORD,
                                                        CSV_DIR,
                                                        SERVER_USERNAME,
                                                        SERVER_NAME,
                                                        DEST_DIRECTORY)
    scp_proc = subprocess.Popen(scp_command, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, shell=True)
    stdout, stderr = scp_proc.communicate()
    print("Output of scp command", stdout, stderr)


def run_rsync_command():
    global RSYNC_SUBPROCESS
    reads_dir = READS_DIR.split("/")[-2]
    # Generate list of rsync options to be used.
    rsync_command_options = []
    # Delete the tar.gz files from the laptop.
    rsync_command_options.append("--remove-source-files")
    rsync_command_options.append("--include='*.tar.gz'")
    rsync_command_options.append("--exclude='*'")  # Exclude everything else!
    rsync_command_options.append("--recursive")
    rsync_command_options.append("--times")

    # Using the 'rsync [OPTION]... SRC [SRC]... [USER@]HOST:DEST'
    # permutation of the command

    # The tar.gz files will be placed in the reads sub folder
    rsync_command = "sshpass -p %s rsync %s %s %s@%s:%s/%s" % (
                                                            PASSWORD,
                                                            ' '.join(rsync_command_options),
                                                            READS_DIR,
                                                            SERVER_USERNAME,
                                                            SERVER_NAME,
                                                            DEST_DIRECTORY,
                                                            reads_dir)

    RSYNC_SUBPROCESS = subprocess.Popen(rsync_command, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, shell=True)


def get_subdirs(run):
    subdirs = [os.path.join(run.fast5_dir, folder)
               for folder in os.listdir(run.fast5_dir)
               # Make sure that the subdirectory is a directory
               if os.path.isdir(os.path.join(run.fast5_dir, folder))
               # And not the tmp directory
               and not folder == "tmp"
               # And subdirectory is int
               and is_int(folder)]

    # Sort subdirectories by writing time.
    return sorted(subdirs, key=os.path.getmtime)


def have_a_break():
    time.sleep(60)


def check_folder_status(subdir, run, full=True):
    subdir_as_standard_int = standardise_int_length(os.path.basename(os.path.normpath(subdir)))
    # Returns the folder status, also generates a little csv file
    # for each of the corresponding folders for you take home.
    fast5_files = [fast5_file for fast5_file in os.listdir(subdir)
                   if fast5_file.endswith(".fast5") and
                   (FLOWCELL in fast5_file or FLOWCELL == "")]

    # Create pandas data frame with each fast5 file as a row.
    # Final columns will include:
    #       fast5 file name       - name of fast5 file
    #       Modification time - time of modification of file,
    #                           useful for deciding if run has finished.
    #       channel           - Channel ID of the run.
    #       read number       - What number read is this.

    fast5_pd = pd.DataFrame(columns=['filename', 'ctime', 'channel', 'read_no'])
    fast5_pd['filename'] = fast5_files
    fast5_pd['ctime'] = [time.ctime(os.path.getmtime(fast5_file))
                         for fast5_file in fast5_files]
    fast5_pd['channel'] = [fast5_file.split('_')[-3]
                           for fast5_file in fast5_files]
    fast5_pd['read_no'] = [fast5_file.split('_')[-2]
                           for fast5_file in fast5_files]

    # If this is the final folder, we will move regardless of if it is full:
    if not full:
        move_fast5_files(subdir, fast5_pd['filename'].tolist(), run)
        fast5_pd.to_csv(os.path.join(run.csv_dir, subdir_as_standard_int + ".csv"))
        return "moving files"
    # Is this a folder with mux scans, if so, we'll move the files over to the
    # actual sequencing run folder
    if run.mux:
        comp_run = get_complementary_run_id(run)
        if comp_run is None:
            # No complementary run, be patient will be there soon!
            return "still writing"
        run.fast5_dir = comp_run.fast5_dir
        run.csv_dir = comp_run.csv_dir
        move_fast5_files(subdir, fast5_pd['filename'].tolist(), run)
        return "moving files"

    if is_folder_maxxed_out(subdir, len(fast5_pd)):
        move_fast5_files(subdir, fast5_pd['filename'].tolist(), run)
        fast5_pd.to_csv(os.path.join(run.csv_dir, subdir_as_standard_int + ".csv"))

    # Check if folder is empty
    fast5_files = [fast5_file for fast5_file in os.listdir(subdir)
                   if fast5_file.endswith(".fast5")]
    if len(fast5_files) == 0:
        subprocess.call("rm -r %s" % subdir, shell=True)

    # Return to the reads directory
    os.chdir(READS_DIR)
    # Used for if we bother trying to tar up in the next step.
    return "still writing"


def get_complementary_run_id(run):
    for other_run in RUNS:
        if other_run == run:
            continue
        if other_run.flowcell == run.flowcell \
                and abs(other_run.start_time - run.start_time) < MUX_PROCESSING_TIME:
            # These are the same run!
            return other_run

    # Didn't find any?
    return None


def is_folder_maxxed_out(subdir, num_files):
    if subdir == "0" and num_files >= 4000:
        return True
    elif subdir != "0" and num_files >= 4001:
        return True
    else:
        return False


def move_fast5_files(subdir, subdir_as_standard_int, fast5_files, run):
    subdir_as_standard_int = standardise_int_length(os.path.basename(os.path.normpath(subdir)))
    if len(fast5_files) == 0:
        return
    mux = ""
    if run.mux:
        mux = "_mux_scan"

    # Create a folder in the reads directory.
    subdir = os.path.join(run.fast5_dir, subdir_as_standard_int)
    new_dir = subdir + "_" + run.random + mux
    os.mkdir(new_dir)

    for fast5_file in fast5_files:
        subprocess.call("mv %s %s" % (fast5_file, new_dir), shell=True)


def tar_folders(subdir_prefix):
    # Get the subdirectories that start with the initial subdirectory.
    # So 0 may now be 0_12345 where 12345 is the rnumber.
    print(subdir_prefix)
    subdirs = [subdir for subdir in os.listdir(READS_DIR)
               if subdir.startswith(subdir_prefix + "_")]
    print(subdirs)
    # Now tar up each folder individually
    for subdir in subdirs:
        tar_file = "%s.tar.gz" % subdir
        tar_command = "tar -cf - %s --remove-files | pigz -9 -p 16 > %s" % (subdir,
                                                                            tar_file)
        tar_proc = subprocess.Popen(tar_command, shell=True,
                                    stderr=subprocess.PIPE, stdout=subprocess.PIPE)
        stdout, stderr = tar_proc.communicate()
        md5sum_tar_file(tar_file)
        if stderr is not None:
            print(stderr)


def md5sum_tar_file(tar_file):
    # Change to parent directory,
    # this is so we have reads/0_12345.tar.gz in the checksums file.
    os.chdir()
    print(PARENT_DIRECTORY)
    
    reads_dir = READS_DIR.split("/")[-2]
 
    md5sum_command = "md5sum %s/%s >> %s" % (reads_dir, tar_file, CHECK_SUMS_FILE)
    # Append the md5sum of the tar file to the list of md5sums.
    checksum_proc = subprocess.Popen(md5sum_command, shell=True,
                                     stderr=subprocess.PIPE, stdout=subprocess.PIPE)
    stdout, stderr = checksum_proc.communicate()
    print("md5sum output", stdout, stderr)

    os.chdir(READS_DIR)    # Change back out of parent directory


def standardise_int_length(my_integer):
    # Input of 15 returns 0015
    return "%04d" % int(my_integer)

main()

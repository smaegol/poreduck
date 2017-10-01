#!/usr/bin/env python

import os
import platform
import pandas as pd
import matplotlib
if platform.system() == 'Linux':
    matplotlib.use('agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
from matplotlib.pylab import savefig
import numpy as np
import sys
import pyjoyplots as pjp
from poreduck.plot_yields import Read_Set
from poreduck.plot_yields import x_hist_to_human_readable
from poreduck.plot_yields import y_yield_to_human_readable
from poreduck.plot_yields import x_yield_to_human_readable

CSV_DIR = ""
PLOTS_DIR = ""
RUNS = []
FASTQ_DIRS = []
NAMES = []
SEQ_DFS = []
CLIP = False

"""
Take two runs, 
create a histogram and a yield comparison between the two runs.
pore duration to come.
Definitely scope for comparing quality as well.
"""


class Run:
    def __init__(self, fastq_dir, name):
        self.all_data = None
        self.yield_data = None
        self.read_sets = []
        self.fastq_dir = fastq_dir
        self.name = name

    def get_read_sets(self):
        # FASTQ_DIR used by readset class.
        fastq_files = [fastq_file for fastq_file in os.listdir(self.fastq_dir)
                       if fastq_file.endswith(".fastq")]
        for fastq_file in fastq_files:
            self.read_sets.append(Read_Set(fastq_dir=self.fastq_dir, fastq_file=fastq_file))

    def get_fastq_data(self):
        for read_set in self.read_sets:
            read_set.read_fastq()

    def aggregate_dataframes(self):
        first_dataframe = True
        for read_set in self.read_sets:
            if first_dataframe:
                first_dataframe = False
                columns = list(read_set.df.columns)
                self.all_data = pd.DataFrame(columns=columns)
            self.all_data = self.all_data.append(read_set.df, ignore_index=True)
        self.all_data = self.all_data.sort_values(['time'], ascending=[True])

    def assign_yield_data(self):
        """We use this for both yield plots"""

        # Read in seq length and time from ALL_READS dataframe
        self.yield_data = self.all_data[['time', "seq_length"]]

        # Aggregate seqlength for each minute of sequencing. I love this resample command!
        self.yield_data.set_index(pd.DatetimeIndex(self.yield_data['time']), inplace=True)
        self.yield_data = self.yield_data.resample("1T").sum()
        self.yield_data.reset_index(inplace=True)
        # Generate a cumulative sum of sequence data
        self.yield_data['cumsum_bp'] = self.yield_data['seq_length'].cumsum()
        # Convert time to timedelta format and then to float format, in hours.
        self.yield_data['duration_tdelta'] = self.yield_data['time'].apply(lambda t: t - self.yield_data['time'].min())
        self.yield_data['duration_float'] = self.yield_data['duration_tdelta'].apply(lambda t: t.total_seconds())


def plot_read_length_hist():
    """For loop of SEQ_DFS here"""
    global SEQ_DFS
    SEQ_DFS = [run.all_data["seq_length"] for run in RUNS]
    # Define how many plots we want (1)
    #fig, ax = plt.subplots(1)
    if CLIP:
        # Filter out the top 1000th percentile.
        """For loop of SEQ_DFS here"""
        for seq_df in SEQ_DFS:
            seq_df = seq_df[seq_df < seq_df.quantile(0.9999)]
    # Merge all the SEQ_DFS.
    all_seq_dfs = pd.DataFrame(data=None, columns=SEQ_DFS[0].columns)
    for index, seq_df in enumerate(SEQ_DFS):
        seq_df["Run"] = RUNS[index].name
        all_seq_dfs.append(seq_df, ignore_index=True)
    ax = pjp.plot(data=all_seq_dfs, x='seq_length', hue='Run', kind="hist")
    # Set the axis formatters
    ax.xaxis.set_major_formatter(FuncFormatter(x_hist_to_human_readable))
    # Set labels of axis.
    ax.set_xlabel("Read length")
    ax.set_ylabel("")
    ax.get_yaxis().set_ticklabels([])

    # Plot the histogram
    #"""For loop here with the ax.hist. with SEQ_DFS"""
    #for (index, seq_df) in enumerate(SEQ_DFS):
    #    ax.hist(seq_df, 50, weights=seq_df,
    #            normed=1, facecolor='blue', alpha=1, label=RUNS[index].name)
    # Set the titles and add a legend.
    title_string = ", ".join([name for name in NAMES[:-1]]) + " and " + NAMES[-1]
    ax.set_title(f"Read Distribution Graph for {title_string}")
    ax.grid(color='black', linestyle=':', linewidth=0.5)
    plt.legend()
    """Need to have another 'regex' name"""
    plot_prefix = '_'.join([name.replace(" ","_") for name in NAMES]))
    savefig(os.path.join(PLOTS_DIR, f"{plot_prefix}_read_length_hist.png"))


def plot_yield_general():
    # Set subplots.
    fig, ax = plt.subplots(1)
    # Create ticks using numpy linspace. Ideally will create 6 points between 0 and 48 hours.
    num_points = 6
    min_x = min([run.yield_data['duration_float'].min() for run in RUNS])
    max_x = max([run.yield_data['duration_float'].max() for run in RUNS])
    x_ticks = np.linspace(min_x,
                          max_x,
                          num_points)
    ax.set_xticks(x_ticks)

    # Define axis formatters
    ax.yaxis.set_major_formatter(FuncFormatter(y_yield_to_human_readable))
    ax.xaxis.set_major_formatter(FuncFormatter(x_yield_to_human_readable))
    # Set x and y labels and limits.
    ax.set_xlabel("Duration (HH:MM)")
    ax.set_ylabel("Yield")
    ax.set_xlim(min_x, max_x)
    title_string = ", ".join([name for name in NAMES[:-1]]) + " and " + NAMES[-1]
    ax.set_title(f"Yield for {title_string} (B/Hour)")
    """For loop here with SEQ_DFS here"""
    for run in RUNS:
        ax.plot(run.yield_data['duration_float'], run.yield_data['cumsum_bp'],
                linestyle="solid", markevery=[], label=run.name)
    plt.legend()
    savefig(os.path.join(PLOTS_DIR, f"{RUNS[0].name}_{RUNS[1].name}_general_yield_plot.png"))


def set_args(args):
    global PLOTS_DIR, CLIP, FASTQ_DIRS, NAMES
    # Check to ensure that all fastq folders are there
    FASTQ_DIRS = [fastq_dir for fastq_dir in args.fastq_dirs.split(",")]
    NAMES = [name for name in args.run_names.split(",")]
    for fastq_dir in FASTQ_DIRS:
        if not os.path.isdir(fastq_dir):
            sys.exit(f"Error, could not find directory {fastq_dir}")
    # Make plots directory if it doesn't exist
    if not os.path.isdir(args.plots_dir):
        os.mkdir(args.plots_dir)
    PLOTS_DIR = args.plots_dir
    if args.clip:
        CLIP = True


def get_runs(args):
    global RUNS
    """ZIP split fastq_dir, name_dir split(",")  RUNS"""
    for fastq_dir, name in zip(FASTQ_DIRS, NAMES):
        RUNS.append(Run(fastq_dir, name))

    # Now load up dataframes.
    for run in RUNS:
        run.get_read_sets()
        run.get_fastq_data()
        run.aggregate_dataframes()
        run.assign_yield_data()


def main(args):
    set_args(args)
    get_runs(args)
    plot_read_length_hist()
    plot_yield_general()


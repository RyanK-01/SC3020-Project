from __future__ import annotations

# import os
# import sys

# from batch import parse_args, run_batch

if __name__ == "__main__":
    # arguments = parse_args()
    # if arguments.batch and os.path.isfile(arguments.batch):
    #     sys.exit(run_batch(arguments))
    # if arguments.batch:
    #     print(f"Batch file not found: {arguments.batch}. Starting the GUI instead.")

    from interface import launch_gui      # imported here so batch mode does not need Tkinter
    launch_gui()
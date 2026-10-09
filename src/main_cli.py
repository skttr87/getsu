"""
Getsu CLI Main Entry Point (getsu-cli.exe).
Console subsystem executable running synchronously in terminals and CI pipelines.
Completely decoupled from GUI single-instance mutex locks.
"""
import os
import sys

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.cli_cleaner import main

if __name__ == "__main__":
    sys.exit(main())


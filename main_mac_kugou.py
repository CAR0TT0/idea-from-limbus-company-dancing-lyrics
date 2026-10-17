"""Dedicated KuGou Mac entry for source and frozen app launches."""
import sys
from main_mac import main

if __name__ == "__main__":
    raise SystemExit(main(["--kugou", *sys.argv[1:]]))

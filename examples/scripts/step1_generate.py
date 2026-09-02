#!/usr/bin/env python3
"""Write the numbers 1..5, one per line, to the given output file."""

import sys

output_path = sys.argv[1]

with open(output_path, "w") as f:
    for n in range(1, 6):
        f.write(f"{n}\n")

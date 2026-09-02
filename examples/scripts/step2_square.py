#!/usr/bin/env python3
"""Square each number from the input file, one per line, to the output file."""

import sys

input_path, output_path = sys.argv[1], sys.argv[2]

with open(input_path) as f:
    numbers = [int(line) for line in f]

with open(output_path, "w") as f:
    for n in numbers:
        f.write(f"{n * n}\n")

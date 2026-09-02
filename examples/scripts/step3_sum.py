#!/usr/bin/env python3
"""Sum the numbers from the input file and write the total to the output file."""

import sys

input_path, output_path = sys.argv[1], sys.argv[2]

with open(input_path) as f:
    total = sum(int(line) for line in f)

with open(output_path, "w") as f:
    f.write(f"{total}\n")

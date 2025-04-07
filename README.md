# Overview

Serious is a command line space repetition program that uses
exponentially increasing review intervals.  It can use a
text-to-speech program to vocalize questions and answers.

By default, the review intervals are (in hours):

```
0
1
2
3
5
9
13
20
30
45
67
99
146
214
315
464
682
1001
1471
2160
```

The intervals and their spacing can be customized with the
`--param-*` flags.

# Quickstart

On first run:

```
python3 serious.py add <csv_file>
```

Here, `csv_file` should have two comma-separated fields: the first
for the question, the second for the answer.  The delimiter can be
changed with the `--delimiter` flag.

Then,

```
python3 serious.py
```

begins the reviews.

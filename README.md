# medrengard-bench examples

Example code from [medrengard-bench](https://github.com/rileylum/medrengard-bench)'s runs of
[SlopCodeBench](https://github.com/SprocketLab/slop-code-bench): for each problem, the run nearest
the median, with its history across the problem's checkpoints. How the runs were made, and every
run's scores, are in the bench's
[`published/`](https://github.com/rileylum/medrengard-bench/tree/main/published) and
[`METHODS.md`](https://github.com/rileylum/medrengard-bench/blob/main/published/METHODS.md).

This branch holds only this file and the licences; the code is on the others.

## Branches and tags

- `<study>/<problem>/<configuration>`: one history, a commit per checkpoint, for example
  `baseline/datagate/baseline`.
  - `baseline`: Claude Opus 5.5 (effort medium) in Claude Code, through Medrengard, one agent step
    per checkpoint. The first commit is the problem's start, holding its assets, if any; each
    later one is the agent's own commit for a checkpoint.
  - `slopcodebench`: the same model in SlopCodeBench's own harness, one run per problem, for the
    `baseline` study's six problems only; a commit per checkpoint of the snapshot it kept.
- `<branch>/checkpoint-<n>`: the commit graded for checkpoint `n`. Compare two to see how the code
  changed as the specification did.

The studies:

- `baseline`: the six problems first chosen to iterate on, five runs each.
- `baseline-added`: six more, chosen by difficulty for this model, five runs each.
- `survey`, `survey-rerun`: one run on every other problem not held out, which chose the added six.

"Nearest the median" means: the run whose solved fraction is nearest the problem's median, then
whose erosion is, then the first repeat.

## Licence

The agents' code: MIT, see [`LICENSE`](LICENSE). The problems' assets in each start commit are
SlopCodeBench's, under the Apache License 2.0: see
[`LICENSE-SLOPCODEBENCH-PROBLEMS`](LICENSE-SLOPCODEBENCH-PROBLEMS). Cite the SlopCodeBench paper
(arXiv 2603.24755) with any results.

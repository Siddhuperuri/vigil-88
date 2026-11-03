# Application soak

- Started: 20261103T091546Z (UTC)
- Duration asked for: 6 minutes (the plan asks for 480)
- Target: installed application
- User journey (every view once a minute): PASSED
- Process tree: bounded on this run

## Figures

- Ran 6 min of the 480 the plan asks for. Samples every 15 s, whole process tree.
- A short run does not show that a long one would pass.
- Total memory: 516MB -> 522MB (+1.2%), peak 528
- Total handles: 3056 -> 3068 (+0.4%), peak 3074
- Total threads: 177 -> 184 (+4.0%), peak 192
- shell memory: 100MB -> 100MB (+0.5%), peak 102
- renderer memory: 127MB -> 128MB (+0.9%), peak 131
- gpu memory: 97MB -> 101MB (+3.5%), peak 102
- engine memory: 129MB -> 130MB (+0.6%), peak 132
- Processes: 6 to 6.
- Verdict on this run: memory and handles stayed bounded.

"""L4 severity engine (06 §8): a verified event in, a score and a band out.

Severity answers "how much does this matter", which is a different question from confidence's
"how sure are we". The two never feed into each other: coupling them would make a dangerous
event look more certain merely because it is dangerous.

The score is explainable as arithmetic. Every factor is recorded with its raw value, weight and
contribution, so an operator who disagrees can see which factor to argue with and which config
key to change.
"""

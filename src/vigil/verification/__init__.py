"""L4 verification engine (06 §6): a candidate in, a verified event or a reason out.

Fails **closed**. An unverifiable candidate is rejected, never promoted, because a false alarm
spends operator trust — the scarcest resource in any monitoring system (01 §6).

Two rules shape every validator here:

  * **Validators cannot promote.** PASS means "I have no objection", never "I am confident".
    Only accumulation can raise confidence; a validator that could add it would be a second,
    hidden promotion path.
  * **Rejections are data.** Every verdict is kept, including passes, and a rejected candidate is
    recorded with its reason. Without that you only ever see what got through, and you are
    tuning suppression blind.
"""

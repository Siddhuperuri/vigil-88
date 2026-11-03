# 0022: Windows Hello is not built; it needs a security decision

- **Status:** **open: not built, waiting on the owner**
- **Date:** 2026-11-03

## What was asked

Windows Hello where technically appropriate, with graceful handling of unsupported devices and platforms.

## Why it is not built

Hello is not a password replacement that the engine can verify. It is a statement from the operating system,
in the shell's process, that *the person at the keyboard just proved who they are*. The engine only ever sees
HTTP from the console. To honour a Hello result the engine would have to unlock a locked console, or sign in a
user, **without the password**, on the word of a caller. Today every route to a session needs the password
(ADR 0016). A route that does not is a change to the security model, and an unsafe design here is worse than none:
anything on the machine able to reach loopback could call it.

Electron also has no Windows Hello API. The options are a native addon over the WinRT `UserConsentVerifier`, or a
PowerShell call into WinRT, which cannot parent its prompt to the window. Either adds a dependency to the shell.

## The decisions needed

1. **What Hello may do.** Unlock a *locked* console of an already signed-in user only (the narrowest, and the
   one the plan names as a convenience), or also sign in.
2. **How the engine trusts the shell.** For example a per-session secret held only by the shell process and
   passed to the engine at start (as the setup token is), so a Hello result is a request the shell signs.
   This must be designed so that a local process which is not the shell cannot unlock.
3. **What happens when Hello is unavailable or removed.** The password must always still work.
4. **The dependency.** A native addon or a helper process, and who maintains it.

## What a safe first version would look like

Convenience unlock for a locked console only; the shell verifies with the OS, then asks the engine to unlock
using a secret that only the shell holds; the lock screen offers the password beside it, always; an unsupported
device shows the password and says nothing about Hello. Signing in from cold would stay password-only.

None of this is built. Nothing in the application claims Hello works.

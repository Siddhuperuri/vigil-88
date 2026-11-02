import { execFileSync } from "node:child_process";

/** Process ids whose command line contains `marker`, found through WMI (Windows only for now). */
export function pidsMatching(marker: string, nameLike = "python*"): number[] {
  const script = `Get-CimInstance Win32_Process | Where-Object { $_.Name -like '${nameLike}' -and $_.CommandLine -like '*${marker.replace(/'/g, "''")}*' } | ForEach-Object { $_.ProcessId }`;
  const out = execFileSync("powershell", ["-NoProfile", "-NonInteractive", "-Command", script], {
    encoding: "utf8",
    windowsHide: true,
  });
  return out
    .split(/\r?\n/)
    .map((line) => Number.parseInt(line.trim(), 10))
    .filter((pid) => Number.isInteger(pid));
}

export function killPids(pids: number[]): void {
  for (const pid of pids) {
    try {
      process.kill(pid);
    } catch {
      // Already gone: that is the outcome we wanted.
    }
  }
}

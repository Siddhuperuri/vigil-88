// A throwaway measurement page, not the console: it signs in through the real setup flow (so the
// session cookie authenticates the streams exactly as the product will) and shows four tiles.
const CAMERAS = ["cam1", "cam2", "cam3", "cam4"];
const HEADERS = { "Content-Type": "application/json", "X-Vigil-Request": "1" };

async function signIn() {
  const status = await (await fetch("/api/v1/auth/status")).json();
  if (!status.setup_required) return;
  const token = await window.vigilShell.setupToken();
  const secret = `${crypto.randomUUID()}-${crypto.randomUUID()}`;
  const response = await fetch("/api/v1/auth/setup", {
    method: "POST",
    headers: HEADERS,
    body: JSON.stringify({ token, username: "spike", password: secret }),
  });
  if (!response.ok) throw new Error(`setup failed: ${response.status}`);
}

signIn().then(() => {
  const wall = document.getElementById("wall");
  let first = true;
  for (const id of CAMERAS) {
    const img = document.createElement("img");
    img.alt = `live stream for ${id}`;
    img.addEventListener("load", () => {
      if (first) {
        first = false;
        document.title = "first-frame"; // the shell logs title changes as milestones
      }
    });
    img.src = `/api/v1/cameras/${id}/stream.mjpeg`;
    wall.append(img);
  }
});

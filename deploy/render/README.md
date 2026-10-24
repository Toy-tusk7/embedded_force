# Host the EmbedForge IDE on Render

The repository's `render.yaml` describes a password-protected web service on Render's free plan: the full IDE, with
live Nemotron runs on your NVIDIA API key and Tavily key, and caps on steps and searches per run.

1. Sign in at https://dashboard.render.com (with GitHub, so Render can read the repository).
2. **New → Blueprint**, pick the repository, and let Render read `render.yaml` from the `main` branch.
3. Render asks for the three secret values. Type them yourself in Render's form, never in a chat or a file:
   - `EMBEDFORGE_PASSWORD`: a password for the IDE (give it to the judges only);
   - `NVIDIA_API_KEY`: your key from build.nvidia.com;
   - `TAVILY_API_KEY`: your Tavily key.
4. **Apply**. The first deploy installs the RP2350 toolchain and compiles its framework (several minutes).
5. Open `https://<service>.onrender.com`; the browser asks for the password (any user name).

The same Blueprint also creates `embedforge-playground`, a static site serving `demo/site` (the Verify Playground:
verify in the visitor's browser on two drivers Nemotron wrote). It needs no keys and does not sleep.

What to expect on the free plan:

- It sleeps after 15 minutes without visitors; the first visit after that waits about a minute.
- Its disk is reset on every restart or redeploy: projects made there do not last.
- 0.1 CPU and 512 MB: an agent run takes about as long as on a PC (the model does the work), a first build of a new
  project 1–3 minutes, rebuilds faster. RP2350 boards (Arduino) build from the prepared toolchain; other boards
  download their toolchain on first use, which is slow there.
- Every run spends your NVIDIA API calls and up to `EMBEDFORGE_TAVILY_MAX_CALLS` Tavily calls; `EMBEDFORGE_MAX_STEPS`
  stops a run. Change both in the service's Environment settings.

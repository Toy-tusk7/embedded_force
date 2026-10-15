# Running EmbedForge on a Nebius AI Cloud GPU VM

One VM does everything: vLLM serves **NVIDIA Nemotron 3 Super** (120B, 12B active) on the VM's GPUs, and the
EmbedForge IDE runs next to it and is reachable in the browser with a password. No Token Factory credits are needed;
you pay for the VM's GPU hours, so **stop the VM whenever you are not using it** (the disk keeps the model).

## 1. Create the VM

In the Nebius AI Cloud console: **Create resource → Virtual machine**.

| Setting | Value |
|---|---|
| Platform | NVIDIA H100 (or H200) |
| Preset | 4 GPUs (the FP8 model is served with tensor parallel 4); on Blackwell GPUs, 2 are enough with the NVFP4 model |
| Boot disk image | Ubuntu for NVIDIA GPUs (CUDA), e.g. Ubuntu 24.04 with CUDA |
| Boot disk size | 500 GiB SSD (model weights and toolchains) |
| Public IP | Auto-assign a static IP |
| SSH key | Your public key (`ssh-keygen -t ed25519` on your PC creates one) |

In the VM's network security settings, allow inbound **TCP 8000** (the IDE) only. Port 8001 (the model server) must
stay closed: it has no password.

## 2. Install vLLM and EmbedForge

```bash
ssh ubuntu@<VM public IP>
sudo apt-get update && sudo apt-get install -y python3-venv git tmux
python3 -m venv ~/vllm && ~/vllm/bin/pip install "vllm>=0.17.1"

git clone https://github.com/Toy-tusk7/embedded_force.git && cd embedded_force
python3 -m venv .venv && .venv/bin/pip install -e .
echo "TAVILY_API_KEY=<your Tavily key>" > .env
```

## 3. Start Nemotron

```bash
tmux new -s nemotron "PATH=~/vllm/bin:\$PATH deploy/nebius-vm/serve-nemotron.sh"
```

The first start downloads the model from Hugging Face (well over 100 GB) and loads it on the GPUs; wait for
`Application startup complete` (detach from tmux with `Ctrl-b d`). Check it:

```bash
curl http://127.0.0.1:8001/v1/models      # lists "nemotron-3-super"
```

On 2 Blackwell GPUs: `MODEL=nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4 TP=2 deploy/nebius-vm/serve-nemotron.sh`.

## 4. Start the IDE

```bash
tmux new -s ide "EMBEDFORGE_PASSWORD='<choose a password>' deploy/nebius-vm/start-embedforge.sh"
```

Open **http://\<VM public IP\>:8000** — the browser asks for a user name (anything) and the password. EmbedForge
uses the VM's Nemotron (provider "Self-hosted"), sends it the Nemotron 3 model card's sampling and chat-template
options, and keeps its projects in `~/embedforge-demo`. Plain HTTP sends the password unencrypted; put a TLS
reverse proxy (e.g. Caddy) in front if you share the link widely.

## 5. Day to day

- **Stop the VM** in the console when you are done; start it again later and re-run steps 3 and 4 (the model is
  already on disk, so it only loads).
- Builds: PlatformIO projects (STM32, ESP32, RP2040/RP2350, …) build on the VM. Betaflight builds need WSL and run
  only on a Windows machine.
- Your laptop does not need to run anything: the IDE on the VM works from any browser, including a tablet.

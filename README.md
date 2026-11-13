# cisco-ai-builder

Type what you want in plain English. A local Ollama model builds the Day 1 lab configs for you.

```
python builder.py "Hey can you build my corebaba, I want to connect the cameras at 10.12.50.6 and 10.12.50.8, block them from wifi"
```

## How it works (the Day 2 lesson)

```
 your prompt ──► Ollama (qwen2.5-coder:7b) ──► JSON plan ──► checks ──► Jinja2 templates ──► config .txt ──► (optional) push
                   "understands you"            pod, IPs,     throw out     the exact Day 1
                                                MACs, ports   made-up values  commands
```

1. **The AI only fills in a plan.** It reads your sentence and answers with JSON: pod, which devices, camera IPs, phone MACs, and so on. Ollama's `format` option forces it to match a schema.
2. **Code checks the plan.** Every IP, MAC, phone number and SSID has to appear in your prompt. Anything the AI made up gets dropped, with a `! AI made up ...` warning.
3. **Templates write the commands.** `templates/*.j2` are the Day 1 configs with `{{ pod }}` instead of `12`. The AI never writes IOS commands, so it can't produce a wrong one.
4. **Push is optional.** Configs always go to `out/podN/`. `--push` sends them over SSH or telnet with Netmiko, and only after you type `yes`.

Classroom demo: run `python builder.py --show-raw "set up corebaba pod 33 with 3 cameras"`.
The raw answer shows the AI inventing camera IPs, and the checks then catching it. That's why we never paste AI output straight into a switch.

## Setup

```
pip install -r requirements.txt
ollama pull qwen2.5-coder:7b
```

## Usage

| Command | What it does |
|---|---|
| `python builder.py "..."` | Build from one prompt |
| `python builder.py` | Interactive: keep typing prompts |
| `--show-raw` | Show the AI's answer before the checks |
| `--pod 12` | Force the pod number |
| `--plan out/pod12/plan.json` | Re-render a saved plan with no AI (fast, works offline) |
| `--push` / `--push --telnet` | Send to the devices (asks first) |
| `--model llama3.1:8b` | Try another Ollama model |

## What it can build

| You say | Device | Management IP |
|---|---|---|
| corebaba, cameras, phones, access point | `COREbaba-P` (VLANs, SVIs, DHCP, access ports, camera reservations, Wi-Fi→camera ACL) | 10.P.1.4 |
| coretaas | `COREtaas-P` (SVIs) | 10.P.1.2 |
| ip phones, cucm, call manager | `CUCM-P` (telephony-service, ephone-dn, ephones) | 10.P.100.8 |
| wifi, access point, aironet | `aironet-P` (SSID, WPA) | 10.P.10.3 |

Lab values (passwords, VLANs, default ports, DNS server, model) are in `lab_defaults.json`.

Prompt tips:
- Give the IPs and MACs. The AI won't guess them, and if it tries, the checks drop them.
- Say "on Fa0/9" to pick a port. Otherwise the Day 1 ports are used (APs Fa0/2 and Fa0/4, cameras Fa0/6 and Fa0/8, phones Fa0/5 and Fa0/7).
- No pod number? It's read from your IPs (10.**12**.50.6 → pod 12).

## Adding a new lab (e.g. EtherChannel, OSPF)

1. Copy the lab's commands into `templates/<name>.j2` and replace the pod number with `{{ pod }}`.
2. Add `<name>` to `ROLES` and `HOSTNAMES` in `builder.py`.
3. Add a line about it to `SYSTEM_PROMPT` so the AI knows when to pick it.

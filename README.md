# cisco-ai-builder

Say what you want in plain English. A local Ollama model reads it, the builder writes the Day 1 lab configs, and SecureCRT types them into the switches over the console cable or telnet.

```
python builder.py "I want to configure my cisco switches. coretaas will have an ip of 10.61.1.2 and corebaba is 10.61.1.4" --securecrt
```

That gives you `COREtaas-61` and `COREbaba-61` with exactly the TASK1 commands from class: basic config, SVIs, trunk + EtherChannel LACP on Fa0/10-12, DHCP pools, VLANs 10/50/69/70/71/100, the access ports (APs, cameras, CUCM, phones), and camera reservations. Then SecureCRT opens on your console cable and types it in for you.

The `m` in `10.m.1.2` is your number. Type the real IP (`10.61.1.2`), or type `10.m.1.2` and add `pod 61`.

**[PROMPTS.md](PROMPTS.md)** lists every prompt that works, what each one builds, and which parts of the lab notes aren't supported yet.

## How it works

```
 your prompt ──► Ollama ──► JSON plan ──► checks + rules ──► Jinja2 templates ──► .txt config
                 "understands you"        throw out made-up     the lab notes'        + .securecrt.py
                                          values                commands              ──► SecureCRT types it
```

1. **The AI only fills in a plan.** Which devices, their IPs, cameras, phones and so on. Ollama's `format` option forces the answer into a JSON schema.
2. **Code checks the plan.** Every IP, MAC and number has to appear in your prompt, or it's dropped with a `!` warning. Plain regex rules also read the prompt (`coretaas ... 10.61.1.2`), so a small model that misses something doesn't break the build. If Ollama is off, the rules alone still build the switches.
3. **Templates write the commands.** `templates/*.j2` are the lab notes with `{{ pod }}` in place of `61`. The AI never writes IOS commands, so it can't produce a wrong one.
4. **SecureCRT types it.** Each device also gets a `<name>.securecrt.py` script. It gets through the fresh-switch setup dialog (answers `no`), logs in, runs `enable`, then sends one line at a time and waits for the `#` prompt before the next line. On a 9600-baud console, a big paste drops lines; this doesn't. At the end it pops up any lines IOS rejected.

## Setup

```
pip install -r requirements.txt
ollama pull qwen2.5-coder:7b
```

Any pulled model works. If `qwen2.5-coder:7b` is missing, the builder uses the best one you have (e.g. `llama3.2`).

## Usage

| Command | What it does |
|---|---|
| `python builder.py "..."` | Build the configs into `out/podN/` |
| `--securecrt` | Open SecureCRT on the console cable and type each config in. It asks you to move the cable between switches |
| `--securecrt --com COM4` | Pick the COM port (auto-detected if there's only one) |
| `--securecrt --via telnet` | Type it over telnet instead (only after the device already has its IP) |
| `--check` | Afterwards, test telnet (TCP 23) to each device |
| `--no-ai` | Skip Ollama, use the rules only (fast, offline) |
| `--show-raw` | Show the AI's answer before the checks |
| `--pod 61` | Force the pod number |
| `--no-save` | Leave out `write memory` |
| `--plan out/pod61/plan.json` | Re-render a saved plan with no AI |
| `--push` / `--push --telnet` | Old way: send it with Netmiko over SSH/telnet (asks first) |
| `--model llama3.2` | Use another Ollama model |

No `--securecrt`? Connect in SecureCRT yourself, then **Script > Run...** and pick `out/pod61/COREtaas-61.securecrt.py`. Or just paste the `.txt`.

## Checking it in SecureCRT

After the script finishes, both switches are reachable:

- **Console cable**: password `pass`, enable `pass`.
- **Telnet** from a PC in VLAN 1 (10.m.1.x): `telnet 10.61.1.2` (COREtaas) and `telnet 10.61.1.4` (COREbaba), password `pass`.
- `show int trunk`, `show etherchannel summary`, `show ip int brief`, `show vlan brief` (they're listed at the bottom of each config).

## What it can build

| You say | Device | Management IP |
|---|---|---|
| switches, coretaas | `COREtaas-m`: SVIs, VLANs, trunk/LACP, default gateway | 10.m.1.2 (or what you type) |
| switches, corebaba, cameras, phones, access point | `COREbaba-m`: everything in TASK1 | 10.m.1.4 (or what you type) |
| ip phones, cucm, call manager | `CUCM-m` (telephony-service, ephone-dn, ephones) | 10.m.100.8 |
| access point, aironet | `aironet-m` (SSID, WPA) | 10.m.10.3 |

Add-ons you can ask for in the same sentence: `with ospf`, `default route`, `ssh`, `cameras at 10.61.50.6 mac 001a.2b3c.4d5e` (makes the MAC reservation), `block the cameras from wifi`, `no etherchannel`.

Lab values (passwords, VLAN names, ports, DNS, SecureCRT path, model) are in `lab_defaults.json`.

## Fixes compared to the raw lab notes

- COREtaas also creates VLANs 10/50/100. Without them its SVIs for those VLANs stay down.
- COREtaas gets `ip default-gateway 10.m.1.4` so you can telnet to it from other VLANs, not only VLAN 1.
- Camera reservations with no MAC become comments, not `client-identifier 001a.xxxx.yyyy` (which IOS rejects).
- `write memory` at the end, so a reload doesn't wipe the lab.

## Adding a new lab (e.g. EDGE, OSPF on the router)

1. Copy the lab's commands into `templates/<name>.j2` and replace the pod number with `{{ pod }}`.
2. Add `<name>` to `ROLES`, `HOSTNAMES` and `NAME_RE` in `builder.py`.
3. Add a line about it to `SYSTEM_PROMPT` so the AI knows when to pick it.

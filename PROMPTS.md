# Prompts

Copy a prompt, put your number where it says `61`, and run it:

```
python builder.py "<prompt>"              # writes the configs to out/pod61/
python builder.py "<prompt>" --securecrt  # also types them in over the console cable
```

You can type `10.m.1.2` instead of `10.61.1.2` if you add `pod 61` to the sentence.

Each prompt below was run through Ollama, and the output was checked against the lab notes.

---

## 1. TASK1: collapsed backbone (both switches)

```
I want to configure my cisco switches. coretaas will have an ip of 10.61.1.2 and corebaba is 10.61.1.4
```

| Lab notes section | Device |
|---|---|
| Basic config (hostname, enable secret, console/vty, no logging, no domain lookup) | both |
| SVIs: VLAN 1 MGMTDATA, 10 WIRELESS, 50 IPCCTV, 100 VOICEVLAN | both |
| Gi0/1 routed uplink `10.61.61.4` | COREbaba |
| Trunk ports Fa0/10-12 | both |
| EtherChannel LACP (`channel-group 1 mode active`) | both |
| DHCP excluded .1-.100 and pools MGMTDATA, WIFIDATA, IPCCTV, VOICEVLAN (option 150) | COREbaba |
| VLANs 10, 50, 69, 70, 71, 100 | COREbaba (10/50/100 on COREtaas too) |
| Access ports: Fa0/2 and 0/4 Wi-Fi, Fa0/6 and 0/8 cameras, Fa0/3 CUCM, Fa0/5 and 0/7 phones | COREbaba |
| `ip routing` | COREbaba |
| `write memory` | both |

The last number of each IP sets that switch's address on every SVI. `coretaas ... 10.61.1.3` puts COREtaas at `.3` everywhere.

Other ways to say it:

```
10.61.1.2 for coretaas and 10.61.1.4 for corebaba
configure my switches pod 61, coretaas is 10.m.1.2 and corebaba 10.m.1.4
configure core taas 10.61.1.2 only
```

To skip the trunk and EtherChannel, add `no etherchannel`.

## 2. Camera MAC reservations

```
configure corebaba 10.61.1.4 with ip cameras at 10.61.50.6 mac 001a.2b3c.4d06 and 10.61.50.8 mac 001a.2b3c.4d08
```

This makes `ip dhcp pool CAMERA6` and `CAMERA8` with `host` and `client-identifier 0100.1a2b.3c4d.06`. Use your camera's real MAC from its sticker or `show mac address-table`.

A camera with no MAC becomes a comment instead. The notes' `client-identifier 001a.xxxx.yyyy` is rejected by IOS, so it isn't used.

Add-ons:

```
... on Fa0/9                         put a camera on a different port
... and block them from wifi         VLAN access-map that drops Wi-Fi to the cameras' web pages
```

## 3. SSH, OSPF and default route on COREbaba

```
configure corebaba 10.61.1.4 with ospf, ssh, and a default route to the edge
```

| Words | Adds |
|---|---|
| `ssh` | `username admin privilege 15 secret pass`, `ip domain-name rivanit.com`, `crypto key generate rsa modulus 1024`, `ip ssh version 2`, vty `login local` |
| `ospf` | `router ospf 1`, `router-id 10.61.61.4`, `network 10.61.0.0 0.0.255.255 area 0`, `ip ospf network point-to-point` on Gi0/1 |
| `default route` / `internet` | `ip route 0.0.0.0 0.0.0.0 10.61.61.1` |

With `ssh`, telnet and SSH both log in as `admin` / `pass`.

## 4. CUCM with IP phones (part of TASK2)

```
set up the cucm for pod 61 with ip phones 6111 mac cafe.face.baba and 6122 mac fafa.caca.baba
```

This builds `CUCM-61`:

- the basic config
- `Fa0/0` at `10.61.100.8`
- a default route to `10.61.100.4`
- `telephony-service` (no auto assign, max-ephones, `ip source-address ... port 2000`, `create cnf-files`)
- one `ephone-dn` and one `ephone` (type 8945) per phone

It also builds COREbaba, since the phones plug into it.

It gives one line button per phone. Your notes have 10 extensions and multi-button phones (`button 1:8 2:7 3:6 4:5`), which the builder doesn't do yet.

You can leave out the phone numbers. It then uses 6111, 6122, ... (pod + 11, 22, ...).

## Everything at once

```
I want to configure my cisco switches. coretaas will have an ip of 10.61.1.2 and corebaba is 10.61.1.4 with ospf, ssh and a default route, cameras at 10.61.50.6 mac 001a.2b3c.4d06 and 10.61.50.8 mac 001a.2b3c.4d08, ip phones 6111 mac cafe.face.baba and 6122 mac fafa.caca.baba
```

---

## Not supported yet

These parts of the lab notes have no template yet. The builder ignores these prompts and builds only what it recognizes. Each one needs a template first (see "Adding a new lab" in the README).

| Lab notes section | Prompt it would use |
|---|---|
| TASK2: analog phones (dial-peer pots) | `cucm analog phones 6100 to 6103 on ports 0/0/0 to 0/0/3` |
| TASK2: 10 ephone-dns, multi-button phones | `cucm with extensions 6111 to 6199, phone cafe.face.baba buttons 8 7 6 5` |
| TASK2: video calls, trusted list for incoming calls | `enable video calls and allow incoming calls on the cucm` |
| TASK2: dial-peers to other pods' CUCMs | `cucm dial-peers to pods 11, 21, 22, 31 ... 92` |
| CUCM OSPF | `cucm with ospf` |
| TASK3: EDGE router (Gi0/0/0, Gi0/0/1, Loopback0) | `configure edge router 10.61.61.1, wan 200.0.0.61, loopback 61.0.0.1` |
| EDGE OSPF and NAT overload | `edge with ospf and nat overload to 200.0.0.1` |
| Static routes to classmates | `edge static routes to pods 11, 21, 22 ... 82 via 200.0.0.x` |
| IVR auto attendant | `set up the ivr auto attendant on 6169 hunting 6100 6177 6101 6133` |
| SIP phones, calling other countries | `sip phones 6171 and 6172 with macs ..., sip server 192.168.k.1` |

Windows Server steps (installing DNS and IIS, turning off the firewall, `route add`) run in PowerShell on the PC, not on a Cisco device, so the builder doesn't cover them.

## Tips

- **Give the real IPs and MACs.** Anything the AI makes up that isn't in your prompt is dropped, with a `!` warning.
- **Check what it understood.** The `=== PLAN ===` lines show the pod, the devices and their IPs. Add `--show-raw` to see the AI's raw answer.
- **No Ollama?** `--no-ai` builds the switches (prompts 1 to 3) with the rules alone. Phones need the AI.
- **Did it work?** Add `--check` to test telnet to each device afterwards.

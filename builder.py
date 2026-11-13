"""
cisco-ai-builder: type what you want in plain English, get Cisco configs.

    prompt  ->  Ollama (fills a JSON plan)  ->  checks  ->  Jinja2 templates  ->  config files  ->  (optional) push

The AI never writes IOS commands. It only fills in the plan (pod, devices, camera IPs...).
The commands come from templates/ (the Day 1 lab), so they are always the commands you teach.

Examples:
    python builder.py "build my corebaba pod 12, cameras at 10.12.50.6 and 10.12.50.8"
    python builder.py                      (interactive: keeps asking for prompts)
    python builder.py --plan out/pod12/plan.json      (skip the AI, re-render a saved plan)
    python builder.py "..." --push         (also send it to the devices, asks first)
"""

import argparse
import ipaddress
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

HERE = Path(__file__).resolve().parent
DEFAULTS = json.loads((HERE / "lab_defaults.json").read_text(encoding="utf-8"))
ROLES = ["corebaba", "coretaas", "cucm", "aironet"]
HOSTNAMES = {"corebaba": "COREbaba-{pod}", "coretaas": "COREtaas-{pod}",
             "cucm": "CUCM-{pod}", "aironet": "aironet-{pod}"}

# ---------------------------------------------------------------------------
# 1. What we ask the AI to fill in. Ollama forces its answer to match this schema.
# ---------------------------------------------------------------------------
NULLABLE_STR = {"type": ["string", "null"]}
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "pod": {"type": ["integer", "null"]},
        "devices": {"type": "array", "items": {"type": "string", "enum": ROLES}},
        "cameras": {"type": "array", "items": {"type": "object", "properties": {
            "ip": {"type": "string"}, "mac": NULLABLE_STR, "port": NULLABLE_STR},
            "required": ["ip", "mac", "port"]}},
        "phones": {"type": "array", "items": {"type": "object", "properties": {
            "number": NULLABLE_STR, "mac": NULLABLE_STR, "port": NULLABLE_STR},
            "required": ["number", "mac", "port"]}},
        "access_points": {"type": "array", "items": {"type": "object", "properties": {
            "ip": NULLABLE_STR, "port": NULLABLE_STR},
            "required": ["ip", "port"]}},
        "wifi_ssid": NULLABLE_STR,
        "wifi_password": NULLABLE_STR,
        "block_cameras_from_wifi": {"type": "boolean"},
        "ssh": {"type": "boolean"},
    },
    "required": ["pod", "devices", "cameras", "phones", "access_points",
                 "wifi_ssid", "wifi_password", "block_cameras_from_wifi", "ssh"],
}

SYSTEM_PROMPT = """You turn a network student's request into a JSON plan for a Cisco lab.
You do NOT write Cisco commands. You only fill in the JSON.

Lab facts:
- Every pod uses 10.P.x.x where P is the pod number (e.g. 10.12.50.6 -> pod 12).
- devices can be: "corebaba" (the core/leaf L3 switch where cameras, phones and APs plug in),
  "coretaas" (the upper core switch), "cucm" (CallManager Express router for IP phones),
  "aironet" (the wireless access point).
- If the user mentions IP phones / telephony / CUCM / call manager, include "cucm" as well as "corebaba".
- If the user mentions wifi / wireless / access point / aironet, include "aironet".
- cameras = IP cameras / CCTV. phones = IP phones. access_points = wifi APs.
- port = a switch port like "Fa0/6" or "Gi0/1" only if the user said one.
- block_cameras_from_wifi = true only if the user asks to block/hide/protect the cameras from wifi users.
- ssh = true only if the user asks for SSH / secure remote access / AAA.

RULES:
- Copy IP addresses, MAC addresses and phone numbers EXACTLY as the user typed them.
- NEVER invent an IP, MAC, port or number. If the user did not give it, use null.
- If the user did not say the pod number, use null.
"""


def ask_ollama(prompt, model, url):
    body = {
        "model": model,
        "stream": False,
        "format": PLAN_SCHEMA,
        "options": {"temperature": 0},
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(url.rstrip("/") + "/api/chat",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            reply = json.loads(r.read())
    except urllib.error.URLError as e:
        sys.exit(f"Can't reach Ollama at {url} ({e.reason}). Is it running? Try: ollama serve")
    return json.loads(reply["message"]["content"])


# ---------------------------------------------------------------------------
# 2. Check the AI's plan. Anything it made up gets thrown out with a warning.
# ---------------------------------------------------------------------------
def hex_only(s):
    return re.sub(r"[^0-9a-f]", "", (s or "").lower())


def clean_mac(mac, prompt, warn, what):
    if not mac:
        return None
    h = hex_only(mac)
    if len(h) != 12:
        warn(f"{what}: '{mac}' is not a valid MAC, ignored")
        return None
    if h not in hex_only(prompt):
        warn(f"{what}: AI made up MAC '{mac}' (not in your prompt), ignored")
        return None
    return f"{h[0:4]}.{h[4:8]}.{h[8:12]}"


def clean_ip(ip, prompt, warn, what):
    if not ip:
        return None
    try:
        ipaddress.IPv4Address(ip.strip())
    except ValueError:
        warn(f"{what}: '{ip}' is not a valid IPv4 address, ignored")
        return None
    if not re.search(rf"(?<![\d.]){re.escape(ip.strip())}(?![\d])", prompt):
        warn(f"{what}: AI made up IP '{ip}' (not in your prompt), ignored")
        return None
    return ip.strip()


def clean_port(port, warn, what):
    if not port:
        return None
    m = re.fullmatch(r"\s*(fa|fastethernet|gi|gigabitethernet)\s*(\d+/\d+(?:/\d+)?)\s*", port, re.I)
    if not m:
        warn(f"{what}: '{port}' doesn't look like a switch port, using the default port")
        return None
    return ("Fa" if m.group(1).lower().startswith("f") else "Gi") + m.group(2)


def check_plan(plan, prompt, pod_override=None):
    """Returns (clean plan, list of warnings)."""
    warnings = []
    warn = warnings.append
    p = {"cameras": [], "phones": [], "access_points": []}

    for i, c in enumerate(plan.get("cameras") or [], 1):
        ip = clean_ip(c.get("ip"), prompt, warn, f"camera {i}")
        if ip:
            p["cameras"].append({"ip": ip, "mac": clean_mac(c.get("mac"), prompt, warn, f"camera {ip}"),
                                 "port": clean_port(c.get("port"), warn, f"camera {ip}")})
    for i, ph in enumerate(plan.get("phones") or [], 1):
        num = (ph.get("number") or "").strip() or None
        if num and (not num.isdigit() or num not in prompt):
            warn(f"phone {i}: AI made up number '{num}', using the default")
            num = None
        p["phones"].append({"number": num, "mac": clean_mac(ph.get("mac"), prompt, warn, f"phone {i}"),
                            "port": clean_port(ph.get("port"), warn, f"phone {i}")})
    for i, ap in enumerate(plan.get("access_points") or [], 1):
        p["access_points"].append({"ip": clean_ip(ap.get("ip"), prompt, warn, f"AP {i}"),
                                   "port": clean_port(ap.get("port"), warn, f"AP {i}")})

    # Pod: --pod wins, then what the AI read, then the 2nd octet of any IP the user typed.
    pod = pod_override or plan.get("pod")
    if pod and str(pod) not in prompt and not pod_override:
        warn(f"AI guessed pod {pod} but it's not in your prompt, working it out from the IPs instead")
        pod = None
    if not pod:
        octets = {int(o) for o in re.findall(r"\b10\.(\d+)\.\d+\.\d+\b", prompt)}
        if len(octets) == 1:
            pod = octets.pop()
        elif len(octets) > 1:
            sys.exit(f"Your IPs point at different pods {sorted(octets)}. Say which pod, e.g. 'pod 12'.")
    if not pod:
        sys.exit("I couldn't tell your pod number. Add it to the prompt, e.g. 'pod 12', or use --pod 12.")
    pod = int(pod)
    if not 1 <= pod <= 254:
        sys.exit(f"Pod {pod} is out of range (1-254).")
    p["pod"] = pod

    # Is each device in the right VLAN subnet for this pod?
    for c in p["cameras"]:
        if not c["ip"].startswith(f"10.{pod}.50."):
            warn(f"camera {c['ip']} is not in the camera VLAN 10.{pod}.50.0/24")
    for ap in p["access_points"]:
        if ap["ip"] and not ap["ip"].startswith(f"10.{pod}.10."):
            warn(f"AP {ap['ip']} is not in the wifi VLAN 10.{pod}.10.0/24")

    # Fill in default ports / phone numbers from the Day 1 lab.
    for key in ("cameras", "phones", "access_points"):
        spare = [x for x in DEFAULTS["default_ports"][key]
                 if x not in {i["port"] for i in p[key]}]
        for item in p[key]:
            if not item["port"]:
                if not spare:
                    sys.exit(f"Too many {key} without a port. Say which port each one is on, e.g. 'on Fa0/9'.")
                item["port"] = spare.pop(0)
    used = {ph["number"] for ph in p["phones"]}
    defaults = [f"{pod}{n}{n}" for n in range(1, 10)]
    for ph in p["phones"]:
        if not ph["number"]:
            ph["number"] = next(n for n in defaults if n not in used)
            used.add(ph["number"])

    p["wifi_ssid"] = plan.get("wifi_ssid") if plan.get("wifi_ssid") and plan["wifi_ssid"] in prompt else None
    p["wifi_password"] = plan.get("wifi_password") if plan.get("wifi_password") and plan["wifi_password"] in prompt else None

    # Small models forget these rules, so code enforces them instead of trusting the AI.
    wanted = set(plan.get("devices") or [])
    if p["cameras"] or p["phones"] or p["access_points"]:
        wanted.add("corebaba")
    if p["phones"]:
        wanted.add("cucm")
    if p["access_points"] or p["wifi_ssid"]:
        wanted.add("aironet")
    p["devices"] = [d for d in ROLES if d in wanted] or ["corebaba"]
    p["block_cameras_from_wifi"] = bool(plan.get("block_cameras_from_wifi"))
    p["ssh"] = bool(plan.get("ssh"))
    return p, warnings


# ---------------------------------------------------------------------------
# 3. Templates write the actual config.
# ---------------------------------------------------------------------------
def mgmt_ip(role, plan, ap_ip=None):
    pod = plan["pod"]
    if role == "aironet":
        return ap_ip
    if role == "cucm":
        return f"10.{pod}.100.{DEFAULTS['host_octet']['cucm']}"
    return f"10.{pod}.1.{DEFAULTS['host_octet'][role]}"


def render(plan):
    """Returns a list of (role, hostname, mgmt_ip, config_text)."""
    env = Environment(loader=FileSystemLoader(HERE / "templates"), undefined=StrictUndefined,
                      trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
    pod = plan["pod"]
    cams = [{**c, "client_id": ("01" + hex_only(c["mac"])) if c["mac"] else None} for c in plan["cameras"]]
    for c in cams:
        if c["client_id"]:
            h = c["client_id"]
            c["client_id"] = ".".join(h[i:i + 4] for i in range(0, 14, 4))
    wifi = {**DEFAULTS["wifi"]}
    wifi["ssid"] = plan["wifi_ssid"] or wifi["ssid"].format(pod=pod)
    wifi["password"] = plan["wifi_password"] or wifi["password"]

    out = []
    for role in plan["devices"]:
        ctx = dict(d=DEFAULTS, pod=pod, ssh=plan["ssh"], cameras=cams, phones=plan["phones"],
                   access_points=plan["access_points"], wifi=wifi,
                   block_cameras_from_wifi=plan["block_cameras_from_wifi"],
                   include_cucm_port="cucm" in plan["devices"] or bool(plan["phones"]),
                   octet=DEFAULTS["host_octet"].get(role))
        if role == "aironet":
            aps = plan["access_points"] or [{"ip": f"10.{pod}.10.3", "port": None}]
            for i, ap in enumerate(aps, 1):
                name = HOSTNAMES[role].format(pod=pod) + (f"-{i}" if len(aps) > 1 else "")
                ip = ap["ip"] or f"10.{pod}.10.3"
                text = env.get_template("aironet.j2").render(**ctx, hostname=name, ap_ip=ip)
                out.append((role, name, ip, text))
            continue
        name = HOSTNAMES[role].format(pod=pod)
        text = env.get_template(f"{role}.j2").render(**ctx, hostname=name)
        out.append((role, name, mgmt_ip(role, plan), text))
    return out


# ---------------------------------------------------------------------------
# 4. Optional: push to the real devices (Netmiko). Always asks first.
# ---------------------------------------------------------------------------
def push(configs, plan, use_telnet):
    try:
        from netmiko import ConnectHandler
    except ImportError:
        sys.exit("Pushing needs Netmiko:  pip install netmiko")

    print("\nAbout to send config to:")
    for _, name, ip, _ in configs:
        print(f"  {name:<16} {ip}  ({'telnet' if use_telnet else 'ssh'})")
    if input("Type 'yes' to push: ").strip().lower() != "yes":
        print("Not pushed. The config files are saved, you can paste them by hand.")
        return

    for _, name, ip, text in configs:
        lines = [l for l in text.splitlines()
                 if l.strip() and not l.strip().startswith("!")
                 and l.strip().lower() not in ("config t", "conf t", "end")]
        dev = {"device_type": "cisco_ios_telnet" if use_telnet else "cisco_ios", "host": ip,
               "username": DEFAULTS["admin_user"],
               "password": DEFAULTS["admin_secret"] if not use_telnet else DEFAULTS["line_password"],
               "secret": DEFAULTS["enable_secret"]}
        print(f"\n--- {name} ({ip}) ---")
        try:
            with ConnectHandler(**dev) as conn:
                conn.enable()
                print(conn.send_config_set(lines, read_timeout=120))
                conn.save_config()
                print(f"OK, {name} configured and saved.")
        except Exception as e:  # keep going with the other devices
            print(f"FAILED on {name}: {e}")


# ---------------------------------------------------------------------------
def build(prompt, args):
    if args.plan:
        raw = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        prompt = raw.pop("_prompt", prompt or "")
        print("Using saved plan, skipping the AI.")
    else:
        print(f"Asking {args.model} ... (first run can take a minute while it loads)")
        raw = ask_ollama(prompt, args.model, args.ollama)

    if args.show_raw:
        print("\nAI's raw answer:\n" + json.dumps(raw, indent=2))
    plan, warnings = check_plan(raw, prompt, args.pod)

    print("\n=== PLAN (what the AI understood) ===")
    print(json.dumps(plan, indent=2))
    for w in warnings:
        print(f"  ! {w}")

    configs = render(plan)
    folder = HERE / "out" / f"pod{plan['pod']}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "plan.json").write_text(json.dumps({"_prompt": prompt, **plan}, indent=2), encoding="utf-8")
    for _, name, _, text in configs:
        (folder / f"{name}.txt").write_text(text, encoding="utf-8")
        if not args.quiet:
            print(f"\n{text}")
    print(f"\nSaved {len(configs)} config(s) + plan.json to {folder}")

    if args.push:
        push(configs, plan, args.telnet)


def main():
    ap = argparse.ArgumentParser(description="Build Cisco lab configs from a plain-English prompt using Ollama.")
    ap.add_argument("prompt", nargs="*", help="what you want, e.g. \"build corebaba pod 12 with cameras 10.12.50.6\"")
    ap.add_argument("--pod", type=int, help="force the pod number")
    ap.add_argument("--model", default=DEFAULTS["model"])
    ap.add_argument("--ollama", default=DEFAULTS["ollama_url"])
    ap.add_argument("--plan", help="re-use a saved plan.json instead of asking the AI")
    ap.add_argument("--show-raw", action="store_true", help="show the AI's answer before checks")
    ap.add_argument("--quiet", action="store_true", help="don't print the configs, only save them")
    ap.add_argument("--push", action="store_true", help="send the config to the devices (asks first)")
    ap.add_argument("--telnet", action="store_true", help="push over telnet instead of SSH")
    args = ap.parse_args()

    prompt = " ".join(args.prompt)
    if prompt or args.plan:
        build(prompt, args)
        return
    print("cisco-ai-builder. Describe what to build (blank line or Ctrl+C to quit).")
    while True:
        try:
            prompt = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not prompt:
            break
        build(prompt, args)


if __name__ == "__main__":
    main()

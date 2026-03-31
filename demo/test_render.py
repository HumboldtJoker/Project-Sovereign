#!/usr/bin/env python3
"""
Beast-side render script. SCP this to Beast and run there.

Converts UI-format workflow to API format, swaps prompts, queues clips.
"""

import json
import os
import re
import sys
import time
import requests

COMFYUI_URL = "http://127.0.0.1:8188"
WORKFLOW_PATH = "/home/thomas/ComfyUI/custom_nodes/ComfyUI-WanVideoWrapper/example_workflows/wanvideo_2_1_14B_T2V_example_03.json"

# Our scenes — just the prompts
SCENES = {
    32: "Extreme cinematic macro close-up of a single small coin resting on a bed of green moss and tiny white wildflowers, warm golden sunlight hitting the coin creating a brilliant specular highlight, a miniature holographic financial analysis report floating just above the coin surface barely visible, shallow depth of field, photorealistic, the beauty in something small and powerful, dew drops on the moss catching light",
}

NEG_PROMPT = "worst quality, low quality, normal quality, watermark, signature, jpeg artifacts, deformed, mutated, disfigured, blurry, cartoon, anime, text, distorted faces, oversaturated, neon colors, fantasy elements"


def load_and_convert_workflow(prompt_text, neg_text=NEG_PROMPT, seed=42):
    """Load UI workflow, convert to API format, swap prompts."""

    with open(WORKFLOW_PATH) as f:
        wf = json.load(f)

    nodes = wf["nodes"]
    links = wf.get("links", [])

    # Build link map: link_id -> (src_node_id, src_slot, dest_node_id, dest_slot, type)
    link_map = {}
    for link in links:
        link_id = link[0]
        link_map[link_id] = {
            "src_node": link[1],
            "src_slot": link[2],
            "dst_node": link[3] if len(link) > 3 else None,
            "dst_slot": link[4] if len(link) > 4 else None,
            "type": link[5] if len(link) > 5 else None,
        }

    # Build API prompt
    api_prompt = {}

    for node in nodes:
        nid = str(node["id"])
        ntype = node["type"]

        if ntype in ("Note", "Reroute"):
            continue

        # Get node spec from ComfyUI
        try:
            resp = requests.get(f"{COMFYUI_URL}/object_info/{ntype}", timeout=5)
            if resp.status_code != 200:
                continue
            spec = resp.json().get(ntype, {}).get("input", {})
        except Exception:
            continue

        inputs = {}

        # Map connections
        for conn in node.get("inputs", []):
            link_id = conn.get("link")
            if link_id is not None and link_id in link_map:
                lk = link_map[link_id]
                inputs[conn["name"]] = [str(lk["src_node"]), lk["src_slot"]]

        # Map widget values to non-connection inputs
        required = spec.get("required", {})
        optional = spec.get("optional", {})

        # Determine which inputs are widgets (not connections)
        connection_names = {c["name"] for c in node.get("inputs", []) if c.get("link") is not None}

        widget_names = []
        for iname, ispec in list(required.items()) + list(optional.items()):
            if iname in connection_names or iname in inputs:
                continue
            if isinstance(ispec, list) and len(ispec) > 0:
                first = ispec[0]
                # Connection types are uppercase strings like "MODEL", "LATENT", etc.
                if isinstance(first, str) and first.isupper() and first not in ("BOOLEAN", "INT", "FLOAT", "STRING", "COMBO"):
                    continue
                # List of options = COMBO
                if isinstance(first, list):
                    widget_names.append(iname)
                    continue
                widget_names.append(iname)

        wv = node.get("widgets_values", [])
        wi = 0
        for wname in widget_names:
            if wi < len(wv):
                inputs[wname] = wv[wi]
                wi += 1

        api_prompt[nid] = {
            "class_type": ntype,
            "inputs": inputs,
        }

    # Swap prompts
    for nid, node in api_prompt.items():
        ct = node["class_type"]
        if ct == "CLIPTextEncode":
            text = node["inputs"].get("text", "")
            if "panda" in text.lower() or "nature" in text.lower() or "waterfall" in text.lower():
                node["inputs"]["text"] = prompt_text
            elif any(neg in text for neg in ["低质量", "worst"]):
                node["inputs"]["text"] = neg_text

    # Set seed on sampler
    for nid, node in api_prompt.items():
        if "Sampler" in node["class_type"]:
            if "seed" in node["inputs"]:
                node["inputs"]["seed"] = seed

    return api_prompt


def queue_and_wait(api_prompt, timeout=600):
    """Queue prompt and wait for completion."""
    resp = requests.post(f"{COMFYUI_URL}/prompt", json={"prompt": api_prompt}, timeout=10)
    data = resp.json()

    if "prompt_id" in data:
        pid = data["prompt_id"]
        print(f"  Queued: {pid}")

        start = time.time()
        while time.time() - start < timeout:
            hist = requests.get(f"{COMFYUI_URL}/history/{pid}", timeout=5).json()
            if pid in hist:
                outputs = hist[pid].get("outputs", {})
                for nid, out in outputs.items():
                    if "videos" in out:
                        for v in out["videos"]:
                            print(f"  Output: {v.get('filename', '?')}")
                return True
            time.sleep(5)
        print("  TIMEOUT")
        return False
    else:
        errors = data.get("node_errors", {})
        for nid, ne in errors.items():
            for e in ne.get("errors", []):
                print(f"  Error node {nid} ({ne.get('class_type','?')}): {e['message']} - {e.get('details','?')}")
        if not errors and "error" in data:
            print(f"  Error: {data['error'].get('message', '?')}")
        return False


if __name__ == "__main__":
    print("Sovereign — Video Render")
    print("=" * 50)

    # Test with scene 32 (coin on moss)
    scene = 32
    prompt = SCENES[scene]
    print(f"\nScene {scene}: {prompt[:60]}...")

    api_prompt = load_and_convert_workflow(prompt, seed=42)

    if "--debug" in sys.argv:
        print(json.dumps({"prompt": api_prompt}, indent=2)[:2000])
    else:
        success = queue_and_wait(api_prompt)
        print(f"\nResult: {'SUCCESS' if success else 'FAILED'}")

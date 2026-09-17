#!/usr/bin/env python3
"""OneDrive configuration tool for Minamo.

Scans Minamo's configurations to detect configured OneDrive storage backends,
displays their token setup status, and guides the user through the Microsoft
Graph OAuth2 authorization code flow to obtain and persist the token.
"""
import os
import sys
import json
import time
import urllib.parse
from pathlib import Path
import httpx

# Ensure we can import from the minamo package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minamo.config.manager import ConfigManager


def main():
    print("=" * 60)
    print("         Minamo OneDrive Configuration Tool")
    print("=" * 60)

    # 1. Load Minamo configuration
    config_dir = os.environ.get("MINAMO_CONFIG_DIR", "config")
    if not Path(config_dir).is_dir():
        print(f"Error: Configuration directory '{config_dir}' not found.")
        print("Please ensure Minamo config is initialized first (run Minamo once to scaffold).")
        sys.exit(1)

    try:
        config_mgr = ConfigManager(config_dir)
    except Exception as e:
        print(f"Error loading Minamo configuration: {e}")
        sys.exit(1)

    # Find OneDrive backends
    onedrive_backends = []
    for backend_id, cfg in config_mgr.backend_configs.items():
        if cfg.get("type") == "onedrive":
            onedrive_backends.append((backend_id, cfg))

    if not onedrive_backends:
        print("\nNo OneDrive storage backends detected in the configuration files.")
        print("To configure one, copy/rename config.example/storage-onedrive.toml to")
        print("config/storage-onedrive.toml and set your Client ID and Client Secret.")
        sys.exit(0)

    # 2. Display Token Statuses
    print("\n[+] Detected OneDrive Storage Backends:\n")
    backends_info = []
    for idx, (b_id, cfg) in enumerate(onedrive_backends, 1):
        client_id = cfg.get("client_id", "")
        client_secret = cfg.get("client_secret", "")
        state_file = cfg.get("state_file", "data/state/onedrive.json")
        root_path = cfg.get("root_path", "/Developing/minamo")

        # Resolve state file
        data_root = config_mgr.settings.data_root
        state_path = Path(state_file)
        if not state_path.is_absolute():
            str_path = str(state_file).replace("\\", "/")
            if str_path.startswith("data/"):
                state_path = Path(str_path)
            else:
                state_path = data_root / state_path

        # Check Token Status
        status = "Not Configured"
        tokens = {}
        if state_path.exists():
            try:
                tokens = json.loads(state_path.read_text(encoding="utf-8"))
                access_token = tokens.get("access_token")
                refresh_token = tokens.get("refresh_token")
                expires_at = tokens.get("expires_at", 0)

                if access_token and refresh_token:
                    remaining = expires_at - time.time()
                    if remaining > 0:
                        status = f"Valid (expires in {int(remaining // 60)} minutes)"
                    else:
                        status = "Expired (automatic refresh available)"
                else:
                    status = "Invalid/Incomplete"
            except Exception:
                status = "Corrupted/Unreadable"

        masked_secret = "*" * len(client_secret) if client_secret else "<NOT SET>"
        masked_client_id = client_id if client_id else "<NOT SET>"

        print(f"  [{idx}] Backend ID: {b_id}")
        print(f"      Client ID:     {masked_client_id}")
        print(f"      Client Secret: {masked_secret}")
        print(f"      Root Path:     {root_path}")
        print(f"      State File:    {state_file}")
        print(f"      Token Status:  {status}")
        print("-" * 50)

        backends_info.append({
            "idx": idx,
            "id": b_id,
            "cfg": cfg,
            "client_id": client_id,
            "client_secret": client_secret,
            "state_path": state_path,
        })

    # 3. Select Backend to Configure
    while True:
        try:
            choice = input("\nSelect a backend to configure [1-{}] (or press Enter to exit): ".format(len(backends_info))).strip()
            if not choice:
                print("Exiting.")
                return
            choice_idx = int(choice)
            if 1 <= choice_idx <= len(backends_info):
                selected = backends_info[choice_idx - 1]
                break
            else:
                print("Invalid index.")
        except ValueError:
            print("Please enter a valid number.")

    client_id = selected["client_id"]
    client_secret = selected["client_secret"]
    state_path = selected["state_path"]

    if not client_id or not client_secret:
        print("\nError: Client ID and Client Secret must be configured in the TOML file before authenticating.")
        print(f"Please edit the TOML config file for backend '{selected['id']}' first.")
        sys.exit(1)

    # 4. Guide User Through OAuth2 Flow
    redirect_uri = "https://login.microsoftonline.com/common/oauth2/v2.0/oauth2play"
    scope = "files.readwrite offline_access"
    auth_base = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"

    params = {
        "client_id": client_id,
        "scope": scope,
        "response_type": "code",
        "redirect_uri": redirect_uri,
    }
    auth_url = f"{auth_base}?{urllib.parse.urlencode(params)}"

    print("\n" + "=" * 60)
    print("                Microsoft Graph OAuth2 Login Guide")
    print("=" * 60)
    print("1. Copy and open the following URL in your web browser:")
    print(f"\n   {auth_url}\n")
    print("2. Log in with your Microsoft account (Personal, Work, or School) and authorize.")
    print("3. After authorization, you will be redirected to a page.")
    print("4. Copy either the entire address bar URL (which contains '?code=...')")
    print("   or just the code itself, and paste it below.")
    print("=" * 60)

    try:
        user_input = input("\nPaste the Redirected URL or Code here: ").strip()
    except KeyboardInterrupt:
        print("\nAborted.")
        return

    if not user_input:
        print("No input provided. Aborting.")
        return

    # Extract authorization code from URL or use as-is
    auth_code = user_input
    if "code=" in user_input:
        try:
            parsed = urllib.parse.urlparse(user_input)
            queries = urllib.parse.parse_qs(parsed.query)
            if "code" in queries:
                auth_code = queries["code"][0]
        except Exception as e:
            print(f"Warning: Failed to parse URL: {e}. Using raw input as code.")

    # 5. Exchange code for tokens
    print("\n[+] Exchanging code for tokens...")
    token_url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    token_data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": auth_code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(token_url, data=token_data)
    except Exception as e:
        print(f"\nError communicating with Microsoft token endpoint: {e}")
        sys.exit(1)

    if resp.status_code != 200:
        print(f"\nFailed to retrieve tokens: Status {resp.status_code}")
        print(resp.text)
        sys.exit(1)

    resp_json = resp.json()
    access_token = resp_json["access_token"]
    refresh_token = resp_json.get("refresh_token")
    expires_in = resp_json.get("expires_in", 3600)
    expires_at = int(time.time()) + expires_in

    if not refresh_token:
        print("\nWarning: No refresh token returned. Offline access might not be configured properly.")

    tokens = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
    }

    # 6. Verify Tokens with Graph API
    print("[+] Verifying token with Microsoft Graph API...")
    verify_url = "https://graph.microsoft.com/v1.0/me/drive"
    verify_headers = {"Authorization": f"Bearer {access_token}"}

    try:
        with httpx.Client(timeout=15.0) as client:
            v_resp = client.get(verify_url, headers=verify_headers)
    except Exception as e:
        print(f"\nError verifying token with Graph API: {e}")
        sys.exit(1)

    if v_resp.status_code != 200:
        print(f"\nToken verification failed with status {v_resp.status_code}:")
        print(v_resp.text)
        sys.exit(1)

    v_json = v_resp.json()
    owner_name = v_json.get("owner", {}).get("user", {}).get("displayName", "Unknown User")
    drive_type = v_json.get("driveType", "Unknown Drive Type")

    print("\n" + "-" * 50)
    print(" [✓] Token Verified Successfully!")
    print(f"     Owner:      {owner_name}")
    print(f"     Drive Type: {drive_type}")
    print("-" * 50)

    # 7. Write Tokens to State File
    print(f"\n[+] Saving tokens to state file '{state_path}'...")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_suffix(".tmp")
    try:
        tmp_path.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
        tmp_path.replace(state_path)
        try:
            os.chmod(state_path, 0o600)
        except OSError:
            pass
        print(" [✓] Token settings successfully saved and secured.")
    except Exception as e:
        print(f"\nError saving state file: {e}")
        sys.exit(1)

    print("\nConfiguration Completed successfully! You can now start Minamo with the OneDrive backend.")


if __name__ == "__main__":
    main()

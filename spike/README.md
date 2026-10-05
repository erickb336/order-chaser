# R10 spike: a Touch ID lock that only our helper can open

This kit tests one question on your Mac: can a signed helper keep a secret in the Keychain behind Touch ID, so that `/usr/bin/security` and Python cannot read it?

The test uses a random SAMPLE value, never a real key. It stores the SAMPLE, tries to read it in several ways, and then deletes it. The result file holds no secret.

You need: a Mac with Touch ID set up, Xcode 27 in `/Applications`, and a free Apple ID. It takes about 10 minutes.

## Steps

1. Open Xcode. Go to **Settings > Accounts**, click **+**, choose **Apple ID** and sign in. Xcode makes a free "Personal Team" for you.

   ```bash
   open -a /Applications/Xcode.app
   ```

2. Tell the command-line tools to use this Xcode. Type your Mac password when asked.

   ```bash
   sudo xcode-select -s /Applications/Xcode.app
   ```

3. Go to the kit's folder.

   ```bash
   cd ~/workspace/order-chaser-spike
   ```

4. Build and sign the helper. On the first build, macOS can ask for your login password so that codesign can use your new signing key. Click **Allow**, not **Always Allow**.

   ```bash
   spike/build.sh
   ```

   It ends with `OK:` and the identity it used. If it says "no team found", copy your team ID from Xcode (**Settings > Accounts > your team**) and run `OC_TEAM_ID=<team ID> spike/build.sh`.

5. Run the test. It stops before each Touch ID step and tells you what to do: touch, touch again, then press **Cancel**.

   ```bash
   spike/owner-test.sh
   ```

   To see the steps first without touching anything, run `spike/owner-test.sh --dry-run`.

6. Send the result file to the chat. It ends with `RESULT: PASS` or `RESULT: FAIL`.

   ```bash
   open -R ~/order-chaser-spike-result.txt
   ```

## Make the signing key ask each time

The build puts an "Apple Development" signing key in your login keychain. Any program that you run could use that key to sign a copy of the helper. Do one of these after the test:

- **Make the key ask each time.** Open **Keychain Access**. Find the certificate "Apple Development: <your name>" and open the arrow to see its private key. Select the key, choose **File > Get Info > Access Control**, select **Confirm before allowing access**, remove every app from the list, and click **Save Changes**.
- **Remove the key.** Delete that private key in Keychain Access. Xcode makes a new one at the next build.

**Residual risk:** a program that runs as you and can use the signing key could sign its own copy of the helper, but that copy still needs your touch, and its Touch ID sheet could show false order text.

## What is in this folder

| File | What it does |
| --- | --- |
| `OCSpike/main.swift` | The helper: `store <service>` (value from stdin, not shown), `read <service> <order-text>` (prints only the byte count), `delete <service>`, `whoami` (signing team and entitlements). |
| `OCSpike/OCSpike.entitlements` | The one entitlement: `keychain-access-groups` = `<team ID>.<bundle id>`. |
| `OCSpike.xcodeproj` | Release build, hardened runtime, automatic signing. The product is `OCSpike.app/Contents/MacOS/oc-spike`. |
| `build.sh` | Builds and signs, then checks the signature, the access group, no `get-task-allow`, and `embedded.provisionprofile`. |
| `owner-test.sh` | The test: steps 0 to 10, PASS or FAIL for each, log in `~/order-chaser-spike-result.txt`. |

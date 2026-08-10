# HARALD — put it on Oracle so the team can use it

HARALD is a Python FastAPI app (GenAI + local embeddings + LibreOffice).
It cannot run in APEX alone. Same hosting model as the SCOUT worker:

**OCI Container Instance** serves the UI and the API on port 8080.
People on the other side of the world open the public URL in a browser.

The database is already ready (`ITERIA_AI`, 16 tables, library loaded).
What remains is: image → registry → container → URL.

This machine has no Docker and no OCI CLI, so the build happens in
**OCI Cloud Shell** (browser, already signed into your tenancy).

---

## What you need open

1. OCI Console (ap-mumbai-1)
2. Cloud Shell (icon in the Console top bar)
3. About 20 minutes

---

## Step 0 — one-time IAM (if SCOUT already has a dynamic group, reuse the pattern)

Create a dynamic group, e.g. `harald-containers`:

```
ALL {resource.type='computecontainerinstance'}
```

Or, tighter, match the specific Container Instance OCID after you create it.

Policy statements (tenancy / root compartment, same place SCOUT’s live):

```
Allow dynamic-group harald-containers to use generative-ai-family in tenancy
Allow dynamic-group harald-containers to manage objects in tenancy where target.bucket.name='FRPStudio'
Allow dynamic-group harald-containers to read objects in tenancy where target.bucket.name='harald-config'
Allow dynamic-group harald-containers to read repos in tenancy
```

`harald-config` is a **private** bucket for the ADB wallet zip only.
Do not put the wallet in `FRPStudio` (that bucket has been public).

---

## Step 1 — private wallet bucket

1. Object Storage → Create Bucket → name `harald-config` → **Private**
2. Upload your ADB `wallet.zip` as object `wallet.zip`
3. Keep FRPStudio for proposals; keep harald-config for secrets only

---

## Step 2 — build and push the image (Cloud Shell)

In Cloud Shell:

```bash
# Get the code onto Cloud Shell. Easiest: zip herald/ on your PC,
# upload via Cloud Shell’s Upload button, then:
cd ~
unzip -o harald-src.zip -d harald-src
cd harald-src   # folder that contains Dockerfile, app/, web/, deploy/

# Auth to OCIR (namespace is already bmi3vxyqnzrv)
docker login bom.ocir.io
# Username: bmi3vxyqnzrv/<your-oci-username-or-email>
# Password: an Auth Token from Identity → your user → Auth Tokens (not your login password)

docker build --platform linux/amd64 -t bom.ocir.io/bmi3vxyqnzrv/harald/harald:latest .
docker push bom.ocir.io/bmi3vxyqnzrv/harald/harald:latest
```

If `docker login` fails, create an Auth Token under your user first.

---

## Step 3 — Container Instance

Console → Developer Services → Container Instances → Create.

| Field | Value |
|---|---|
| Name | `harald` |
| Compartment | root / same as SCOUT |
| Availability domain | any |
| Shape | 2 OCPU / 16 GB (embeddings + LibreOffice need RAM) |
| VCN / subnet | one that can reach ATP (same as SCOUT’s CI) |
| Public IP | **Yes** (so people abroad can open it) |
| Image | `bom.ocir.io/bmi3vxyqnzrv/harald/harald:latest` |
| Image pull | OCIR, use your Auth Token / image pull secret |
| Port | 8080 |

### Environment variables

**None.** Same as SCOUT: DB password, GenAI OCIDs, bucket, wallet location and
session signing key are baked into the image. Create the instance, pick the
image, open the port. Sign-in is pick-a-name — no approver passphrase.

Override an env var on the instance only if a value actually changes.

---

## Step 4 — open the firewall

Security list / NSG on that subnet: ingress **TCP 8080** from `0.0.0.0/0`
(or from your office IP ranges if you want it locked down).

ATP network access must already allow the Container Instance subnet
(same rule set SCOUT uses).

---

## Step 5 — smoke test

After the instance shows Running:

```text
http://<PUBLIC_IP>:8080/api/health
http://<PUBLIC_IP>:8080/
```

Health should return OK. The home page is FRP Studio sign-in.
Seed users are in `harald_users` (from schema deploy).

Give the team: `http://<PUBLIC_IP>:8080/`

---

## What “done” looks like

- [ ] Image in `bom.ocir.io/bmi3vxyqnzrv/harald/harald:latest`
- [ ] Container Instance `harald` Running
- [ ] `/api/health` returns 200 from the public IP
- [ ] Sign-in works
- [ ] Create opportunity → upload RFP → generate produces a draft
- [ ] `FRPStudio` bucket set back to **Private**

---

## HTTPS (later, not required to go live)

Container Instance public IP is HTTP. For proper HTTPS, put a
Flexible Load Balancer (or API Gateway) in front with a cert and
forward to port 8080. Do that after the HTTP smoke test passes.

---

## APEX?

APEX is optional for HARALD. Unlike SCOUT, this container already serves
the HTML. You do **not** need to upload pages into APEX for people to use it.
If you later want the link under the APEX domain, we can reverse-proxy;
that is polish, not a go-live requirement.

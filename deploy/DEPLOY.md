# HARALD — put it on Oracle

Same as SCOUT: image in OCIR → Container Instance → people open the URL.

You do **not** set env vars. The image already has the database wallet,
passwords, and GenAI settings.

## Create the Container Instance

Console → Developer Services → Container Instances → Create.

| Field | Value |
|---|---|
| Name | `harald` |
| Image | `bom.ocir.io/bmi3vxyqnzrv/harald/harald:latest` |
| Public IP | Yes |
| Port | **8000** (same as your other containers) |
| Shape | 2 OCPU / 16 GB |
| VCN / subnet | same as SCOUT |

Open firewall / NSG: TCP **8000**.

## After every push to `main`

GitHub Actions builds and pushes `bom.ocir.io/bmi3vxyqnzrv/harald/harald:latest`.
The running Container Instance does **not** pull it automatically.

1. Console → Developer Services → **Container Instances** → `harald`
2. **Stop** the instance (or delete and recreate with the same settings)
3. **Start** / **Create** again using image `bom.ocir.io/bmi3vxyqnzrv/harald/harald:latest`
4. Confirm health: `http://<PUBLIC_IP>:8000/api/health`

Until you restart, Generate can finish with an empty draft and `ORA-02290` in logs
(old code wrote requirement status `review` instead of `reviewed` when auto-humanize runs).

## Done when

`http://<PUBLIC_IP>:8000/api/health` returns OK  
`http://<PUBLIC_IP>:8000/` is the sign-in screen (pick a name)

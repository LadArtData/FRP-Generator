# HARALD — put it on Oracle

Same as SCOUT: image in OCIR → Container Instance → people open the URL.

You do **not** set env vars. You do **not** upload a wallet bucket.
The image already has the database wallet, passwords, and GenAI settings.

## Create the Container Instance

Console → Developer Services → Container Instances → Create.

| Field | Value |
|---|---|
| Name | `harald` |
| Image | `bom.ocir.io/bmi3vxyqnzrv/harald/harald:latest` |
| Public IP | Yes |
| Port | 8080 |
| Shape | 2 OCPU / 16 GB |
| VCN / subnet | same as SCOUT |

Open firewall / NSG: TCP **8080**.

## Done when

`http://<PUBLIC_IP>:8080/api/health` returns OK  
`http://<PUBLIC_IP>:8080/` is the sign-in screen (pick a name)

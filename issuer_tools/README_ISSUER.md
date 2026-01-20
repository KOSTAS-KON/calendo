Issuer tools (vendor only)

1) Create a keypair once (keep the private key secret):
   python generate_keypair.py

2) Copy the printed PUBLIC key into the product's `.env` as `LICENSE_PUBLIC_KEY=...`

3) Generate an activation code for a client:
   python generate_license.py --private-key private_key.b64 --client-id CLIENT_A --plan 1 --mode BOTH

Plan mapping: 1 = 1 week trial, 2 = 1 month trial, 3 = 1 year license

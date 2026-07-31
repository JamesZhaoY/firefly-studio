# Fly.io volume init script
# Use after `fly launch` to create the persistent volume and seed credentials.

# 1. Create 1GB volume (free)
fly volumes create firefly_data --size 1 --region nrt

# 2. Upload Adobe credentials as secrets (run from your local machine)
fly secrets set STORAGE_JSON="$(cat data/storage.json)"
fly secrets set TOKEN_JSON="$(cat data/current_token.json)"

# 3. Deploy
fly deploy
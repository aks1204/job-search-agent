# Seed packs

Curated, optional company lists. Turn packs on or off in `config/scouts.yaml` under `packs.enabled_packs`.

Pack companies enter the same pipeline as everything else: they're judged by your rules, and being in a pack is not a pass. Your own `config/seed_companies.yaml` always takes precedence, and pack companies queue for judgment after your seeds, HN, YC and the job sites.

To add a pack, create `config/seed_packs/<name>.yaml`:

```yaml
name: "Human-readable pack name"
description: "What's in it and why"
companies:
  - {name: "Example Co", domain: "example.com", type: established}   # or: startup
```

The domain is the company's identity in the agent, so use its official website domain. Contributions are welcome.

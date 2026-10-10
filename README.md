# Walk Journal 🌾

**A quest-giving outdoor journal.** AI finds real walks near you, writes seasonal, place-specific photo quests, reads them aloud so your phone goes in your pocket, then checks your photo proof on your own laptop. Every completed walk becomes a pin on your map and a page in your journal.

> Built for the DEV Hacktoberfest 2026 Open-Source AI Challenge, Week 1: **Touch Grass**.

_Work in progress. Full docs coming as features land._

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in keys
ollama pull gemma4:e4b
python -m basecamp.check      # sanity check
```

## License

MIT

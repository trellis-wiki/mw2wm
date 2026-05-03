#!/usr/bin/env python3
"""Fetch a test corpus of pages and templates from multiple MediaWiki wikis.

Downloads raw wikitext via the MediaWiki API, resolves template chains,
and organizes everything into a directory structure suitable for mw2wm
conversion testing.

Usage:
    python tests/fetch_corpus.py [--output tests/corpus]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "mw2wm-test-corpus/1.0 (https://github.com/trellis-wiki/mw2wm)"

# Rate limit: wait between requests to be polite
RATE_LIMIT = 0.5  # seconds between requests per wiki


# ── Wiki definitions ──────────────────────────────────────────────

WIKIS = {
    "wikipedia": {
        "api": "https://en.wikipedia.org/w/api.php",
        "pages": [
            # People (diverse eras, fields)
            "Ada Lovelace", "Albert Einstein", "Marie Curie", "Leonardo da Vinci",
            "Cleopatra", "Genghis Khan", "Nikola Tesla", "Frida Kahlo",
            "Nelson Mandela", "Mahatma Gandhi", "Martin Luther King Jr.",
            "Alexander the Great", "Queen Victoria", "Napoleon", "Julius Caesar",
            "William Shakespeare", "Mozart", "Aristotle", "Isaac Newton",
            "Charles Darwin", "Alan Turing", "Rosalind Franklin",
            "Galileo Galilei", "Copernicus", "Archimedes",
            "Muhammad Ali", "Pelé", "Serena Williams", "Simone Biles",
            "Usain Bolt",
            # Places
            "Tokyo", "New York City", "London", "Paris", "Rome",
            "Cairo", "Sydney", "Rio de Janeiro", "Moscow", "Berlin",
            "Istanbul", "Bangkok", "Mumbai", "Shanghai", "Nairobi",
            "Buenos Aires", "Toronto", "Lagos", "Singapore", "Dubai",
            "San Francisco", "Chicago", "Los Angeles", "Mexico City",
            "Jerusalem",
            # Countries
            "United States", "Japan", "Germany", "Brazil", "India",
            "Australia", "Canada", "France", "South Korea", "Nigeria",
            "Egypt", "Italy", "United Kingdom", "China", "Russia",
            "South Africa", "Argentina", "Indonesia", "Turkey", "Mexico",
            # Science & tech
            "Python (programming language)", "Linux", "World Wide Web",
            "DNA", "General relativity", "Quantum mechanics",
            "Photosynthesis", "Climate change", "Artificial intelligence",
            "Blockchain", "CRISPR gene editing", "Black hole",
            "Periodic table", "Evolution", "Plate tectonics",
            "Hubble Space Telescope", "International Space Station",
            "Mars", "Jupiter", "Saturn",
            # History
            "World War II", "World War I", "French Revolution",
            "American Civil War", "Cold War", "Renaissance",
            "Industrial Revolution", "Roman Empire", "Byzantine Empire",
            "Mongol Empire", "Ottoman Empire", "British Empire",
            "Ancient Egypt", "Ancient Greece", "Ancient Rome",
            "Crusades", "Viking Age", "Silk Road",
            "Apollo 11", "Chernobyl disaster",
            # Arts & culture
            "Mona Lisa", "Starry Night", "Hamlet",
            "The Lord of the Rings", "Harry Potter",
            "Star Wars", "The Beatles", "Jazz",
            "Impressionism", "Renaissance art",
            "Olympic Games", "FIFA World Cup",
            "Chess", "Go (game)", "Minecraft",
            # Nature
            "Lion", "Blue whale", "Tyrannosaurus",
            "Great Barrier Reef", "Amazon rainforest",
            "Mount Everest", "Grand Canyon", "Sahara",
            "Elephant", "Octopus", "Eagle",
            "Oak", "Sequoia", "Coral reef",
            # Food & everyday
            "Coffee", "Tea", "Chocolate", "Sushi",
            "Pizza", "Beer", "Wine", "Rice",
            # Math & philosophy
            "Pythagorean theorem", "Euler's identity",
            "Calculus", "Set theory", "Philosophy",
            "Existentialism", "Stoicism", "Utilitarianism",
            # Music
            "Beethoven", "Bach", "Bob Dylan",
            "Classical music", "Hip hop music", "Rock music",
            # Medicine
            "Penicillin", "Vaccination", "Cancer",
            "Heart", "Brain", "Immune system",
            # Engineering
            "Bridge", "Skyscraper", "Nuclear power",
            "Electric vehicle", "Airplane", "Submarine",
        ],
        "templates": [
            "Template:Infobox", "Template:Infobox person",
            "Template:Infobox country", "Template:Infobox settlement",
            "Template:Infobox film", "Template:Infobox album",
            "Template:Infobox company", "Template:Infobox book",
            "Template:Cite web", "Template:Cite book",
            "Template:Cite journal", "Template:Cite news",
            "Template:Reflist", "Template:Refn", "Template:Efn",
            "Template:Hatnote", "Template:Main", "Template:See also",
            "Template:Short description", "Template:About",
            "Template:Redirect", "Template:Distinguish",
            "Template:Quote", "Template:Blockquote",
            "Template:Convert", "Template:Coord",
            "Template:Birth date and age", "Template:Death date and age",
            "Template:Flagicon", "Template:Flag",
            "Template:Lang", "Template:IPA",
            "Template:Navbox", "Template:Sidebar",
            "Template:Authority control", "Template:Wikidata",
            "Template:Sfn", "Template:Harvnb",
            "Template:Unbulleted list", "Template:Hlist",
            "Template:Flatlist", "Template:Plainlist",
            "Template:Columns-list",
            "Template:Nowrap", "Template:Nobr",
            "Template:Em dash", "Template:En dash",
            "Template:Spaced en dash",
            "Template:TOC limit", "Template:TOC right",
        ],
    },
    "memory_alpha": {
        "api": "https://memory-alpha.fandom.com/api.php",
        "pages": [
            "Jean-Luc Picard", "James T. Kirk", "Spock", "Data (android)",
            "Worf", "USS Enterprise (NCC-1701)", "USS Enterprise (NCC-1701-D)",
            "Klingon", "Vulcan", "Romulan", "Borg", "Q (species)",
            "Star Trek: The Next Generation", "Star Trek: Deep Space Nine",
            "United Federation of Planets", "Starfleet",
            "Bajoran wormhole", "Deep Space 9", "Holodeck",
            "Phaser", "Warp drive", "Transporter",
            "William Riker", "Deanna Troi", "Beverly Crusher",
            "Seven of Nine", "Kathryn Janeway", "Benjamin Sisko",
            "Cardassian", "Ferengi", "Andorian", "Trill",
            "Dominion", "Species 8472", "Tribble",
            "Battle of Wolf 359", "Kobayashi Maru",
            "Prime Directive", "Starfleet Academy",
            "Bat'leth", "Tricorder", "Communicator",
            "Replicator", "Dilithium", "Photon torpedo",
            "Earth", "Vulcan (planet)", "Qo'noS",
            "Romulus", "Bajor", "Risa",
        ],
        "templates": [
            "Template:Sidebar individual",
            "Template:Sidebar planet",
            "Template:Sidebar starship",
            "Template:Sidebar species",
            "Template:Sidebar episode",
            "Template:Dis", "Template:Bginfo",
        ],
    },
    "wookieepedia": {
        "api": "https://starwars.fandom.com/api.php",
        "pages": [
            "Anakin Skywalker", "Luke Skywalker", "Darth Vader",
            "Obi-Wan Kenobi", "Yoda", "Palpatine",
            "Han Solo", "Leia Organa", "Chewbacca",
            "Ahsoka Tano", "Din Djarin", "Grogu",
            "Lightsaber", "Force", "Death Star",
            "Millennium Falcon", "X-wing starfighter",
            "Galactic Empire", "Rebel Alliance", "Galactic Republic",
            "Jedi", "Sith", "Mandalorian",
            "Tatooine", "Coruscant", "Naboo", "Hoth", "Endor",
            "Clone Wars", "Battle of Yavin", "Battle of Endor",
            "Stormtrooper", "Clone trooper", "Droid",
            "R2-D2", "C-3PO", "BB-8",
            "Darth Maul", "Count Dooku", "General Grievous",
            "Boba Fett", "Jabba the Hutt", "Mace Windu",
            "Padmé Amidala", "Kylo Ren", "Rey",
            "Finn", "Poe Dameron",
            "Star Wars: Episode IV A New Hope",
            "Star Wars: Episode V The Empire Strikes Back",
            "Star Wars: Episode VI Return of the Jedi",
        ],
        "templates": [
            "Template:Character infobox",
            "Template:Planet infobox",
            "Template:Ship infobox",
            "Template:Weapon infobox",
            "Template:Scroll box",
        ],
    },
    "lotr_wiki": {
        "api": "https://lotr.fandom.com/api.php",
        "pages": [
            "Gandalf", "Frodo Baggins", "Aragorn", "Legolas", "Gimli",
            "Sauron", "Saruman", "Gollum", "Bilbo Baggins",
            "Samwise Gamgee", "Boromir", "Faramir",
            "Elrond", "Galadriel", "Arwen", "Éowyn", "Théoden",
            "The One Ring", "Sting (sword)", "Andúril",
            "Mordor", "Rivendell", "Gondor", "Rohan", "The Shire",
            "Minas Tirith", "Isengard", "Helm's Deep", "Mount Doom",
            "Hobbit", "Elf (Middle-earth)", "Dwarf (Middle-earth)",
            "Orc", "Nazgûl", "Ent", "Balrog", "Uruk-hai",
            "Battle of the Pelennor Fields", "Battle of the Five Armies",
            "War of the Ring", "Fellowship of the Ring",
        ],
        "templates": [
            "Template:Infobox character",
            "Template:Infobox location",
            "Template:Infobox object",
        ],
    },
    "runescape": {
        "api": "https://runescape.wiki/api.php",
        "pages": [
            "Combat", "Magic", "Ranged", "Prayer", "Summoning",
            "Fishing", "Mining", "Woodcutting", "Farming", "Herblore",
            "Abyssal whip", "Godsword", "Dragon scimitar",
            "Bandos armour", "Barrows equipment",
            "Grand Exchange", "Wilderness", "God Wars Dungeon",
            "Lumbridge", "Varrock", "Falador",
            "TzTok-Jad", "Corporeal Beast", "Nex",
            "Slayer", "Dungeoneering", "Invention",
            "Quest", "Achievement", "Minigame",
        ],
        "templates": [
            "Template:Infobox Monster",
            "Template:Infobox Item",
            "Template:Infobox Bonuses",
        ],
    },
    "minecraft": {
        "api": "https://minecraft.wiki/api.php",
        "pages": [
            "Creeper", "Enderman", "Zombie", "Skeleton", "Spider",
            "Ender Dragon", "Wither", "Warden",
            "Diamond", "Iron Ingot", "Gold Ingot", "Netherite Ingot",
            "Enchanting", "Brewing", "Redstone circuits",
            "The Nether", "The End", "Overworld",
            "Village", "Stronghold", "Nether Fortress",
            "Steve", "Alex", "Villager",
            "Crafting", "Smelting", "Trading",
            "Sword", "Pickaxe", "Bow",
        ],
        "templates": [
            "Template:BlockSprite",
            "Template:ItemSprite",
            "Template:EntitySprite",
        ],
    },
}


def api_get(api_url: str, params: dict) -> dict | None:
    params.setdefault("format", "json")
    try:
        resp = SESSION.get(api_url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, json.JSONDecodeError) as e:
        print(f"  WARNING: API error: {e}", file=sys.stderr)
        return None


def fetch_page_wikitext(api_url: str, title: str) -> str | None:
    """Fetch raw wikitext for a page."""
    data = api_get(api_url, {
        "action": "query",
        "titles": title,
        "prop": "revisions",
        "rvprop": "content",
        "rvslots": "main",
    })
    if not data:
        return None
    pages = data.get("query", {}).get("pages", {})
    for page in pages.values():
        if "missing" in page:
            return None
        revisions = page.get("revisions", [])
        if revisions:
            slots = revisions[0].get("slots", {})
            main = slots.get("main", {})
            return main.get("*") or main.get("content")
    return None


def fetch_templates_used(api_url: str, title: str) -> list[str]:
    """Get list of templates used by a page."""
    templates = []
    params = {
        "action": "query",
        "titles": title,
        "prop": "templates",
        "tllimit": "500",
    }
    while True:
        data = api_get(api_url, params)
        if not data:
            break
        pages = data.get("query", {}).get("pages", {})
        for page in pages.values():
            for tpl in page.get("templates", []):
                templates.append(tpl["title"])
        cont = data.get("continue")
        if cont and "tlcontinue" in cont:
            params["tlcontinue"] = cont["tlcontinue"]
        else:
            break
    return templates


def safe_filename(title: str) -> str:
    """Convert a page title to a safe filename."""
    name = title.replace("/", "∕").replace(":", "_").replace(" ", "_")
    name = re.sub(r'[<>"|?*]', "_", name)
    return name


def fetch_wiki(wiki_id: str, wiki_config: dict, output_dir: Path) -> dict:
    """Fetch all pages and templates for a wiki. Returns stats."""
    api_url = wiki_config["api"]
    pages_dir = output_dir / "pages" / wiki_id
    templates_dir = output_dir / "pages" / wiki_id / "Template"
    pages_dir.mkdir(parents=True, exist_ok=True)
    templates_dir.mkdir(parents=True, exist_ok=True)

    stats = {"pages_fetched": 0, "pages_failed": 0,
             "templates_fetched": 0, "templates_failed": 0,
             "templates_discovered": 0}

    # Track all templates we need to fetch
    needed_templates: set[str] = set()
    fetched_templates: set[str] = set()

    # Add explicitly listed templates
    for tpl in wiki_config.get("templates", []):
        needed_templates.add(tpl)

    # Fetch content pages
    print(f"\n  Fetching {len(wiki_config['pages'])} content pages...")
    for title in wiki_config["pages"]:
        fname = safe_filename(title) + ".wikitext"
        fpath = pages_dir / fname

        if fpath.exists():
            stats["pages_fetched"] += 1
            # Still discover templates from cached pages
            wt = fpath.read_text(encoding="utf-8")
            if wt:
                tpls = fetch_templates_used(api_url, title)
                needed_templates.update(tpls)
                time.sleep(RATE_LIMIT)
            continue

        wikitext = fetch_page_wikitext(api_url, title)
        time.sleep(RATE_LIMIT)

        if wikitext is None:
            print(f"    MISS: {title}")
            stats["pages_failed"] += 1
            continue

        fpath.write_text(wikitext, encoding="utf-8")
        stats["pages_fetched"] += 1

        # Discover templates
        tpls = fetch_templates_used(api_url, title)
        needed_templates.update(tpls)
        time.sleep(RATE_LIMIT)

        if stats["pages_fetched"] % 10 == 0:
            print(f"    {stats['pages_fetched']} pages fetched, "
                  f"{len(needed_templates)} templates discovered...")

    stats["templates_discovered"] = len(needed_templates)
    print(f"  {stats['pages_fetched']} pages fetched, "
          f"{stats['pages_failed']} failed")
    print(f"  {len(needed_templates)} templates discovered")

    # Fetch templates (up to 2 levels of dependencies)
    for depth in range(3):
        to_fetch = needed_templates - fetched_templates
        if not to_fetch:
            break
        print(f"  Fetching templates (depth {depth}): {len(to_fetch)} to go...")
        new_templates: set[str] = set()

        for title in sorted(to_fetch):
            tpl_name = title
            if tpl_name.startswith("Template:"):
                tpl_name = tpl_name[len("Template:"):]
            fname = safe_filename(tpl_name) + ".wikitext"
            fpath = templates_dir / fname

            fetched_templates.add(title)

            if fpath.exists():
                stats["templates_fetched"] += 1
                continue

            wikitext = fetch_page_wikitext(api_url, title)
            time.sleep(RATE_LIMIT)

            if wikitext is None:
                stats["templates_failed"] += 1
                continue

            fpath.write_text(wikitext, encoding="utf-8")
            stats["templates_fetched"] += 1

            # Discover sub-templates
            sub_tpls = fetch_templates_used(api_url, title)
            new_templates.update(sub_tpls)
            time.sleep(RATE_LIMIT)

            if stats["templates_fetched"] % 20 == 0:
                print(f"    {stats['templates_fetched']} templates fetched...")

        needed_templates.update(new_templates)

    print(f"  {stats['templates_fetched']} templates fetched, "
          f"{stats['templates_failed']} failed")

    return stats


def write_manifest(output_dir: Path, all_stats: dict):
    """Write a manifest file describing the corpus."""
    manifest = {
        "version": 1,
        "description": "mw2wm test corpus — pages and templates from multiple MediaWiki wikis",
        "wikis": {},
    }
    for wiki_id, stats in all_stats.items():
        wiki_dir = output_dir / "pages" / wiki_id
        page_count = len(list(wiki_dir.glob("*.wikitext")))
        tpl_count = len(list((wiki_dir / "Template").glob("*.wikitext"))) if (wiki_dir / "Template").is_dir() else 0
        manifest["wikis"][wiki_id] = {
            "api": WIKIS[wiki_id]["api"],
            "pages": page_count,
            "templates": tpl_count,
            "fetch_stats": stats,
        }

    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


def main():
    parser = argparse.ArgumentParser(description="Fetch mw2wm test corpus")
    parser.add_argument("--output", type=Path, default=Path("tests/corpus"),
                        help="Output directory (default: tests/corpus)")
    parser.add_argument("--wiki", type=str, default=None,
                        help="Fetch only this wiki (default: all)")
    parser.add_argument("--skip-templates", action="store_true",
                        help="Skip template dependency resolution")
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)

    wikis_to_fetch = {args.wiki: WIKIS[args.wiki]} if args.wiki else WIKIS
    all_stats = {}

    for wiki_id, wiki_config in wikis_to_fetch.items():
        print(f"\n{'='*60}")
        print(f"Fetching: {wiki_id} ({wiki_config['api']})")
        print(f"  {len(wiki_config['pages'])} pages, "
              f"{len(wiki_config.get('templates', []))} explicit templates")
        stats = fetch_wiki(wiki_id, wiki_config, args.output)
        all_stats[wiki_id] = stats

    write_manifest(args.output, all_stats)

    print(f"\n{'='*60}")
    print("CORPUS SUMMARY")
    total_pages = sum(s["pages_fetched"] for s in all_stats.values())
    total_tpls = sum(s["templates_fetched"] for s in all_stats.values())
    print(f"  Total pages: {total_pages}")
    print(f"  Total templates: {total_tpls}")
    print(f"  Output: {args.output}")


if __name__ == "__main__":
    main()

"""Check the committed catalog against a reviewable checkout of official docs."""
import argparse
import json
from pathlib import Path

from generate_catalog import generate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', default='.cache/bybit-docs')
    parser.add_argument('--catalog', default='bybit_mcp/catalog.json')
    parser.add_argument('--report', help='Optional JSON report of Markdown/Explorer constraint differences')
    args = parser.parse_args()
    committed = json.loads(Path(args.catalog).read_text(encoding='utf-8'))
    generated = generate(Path(args.source))
    differences = {entry['id']: entry['schema_differences'] for entry in generated['endpoints'] if entry.get('schema_differences')}
    summary = {'revision': generated['revision'], 'count': generated['count'],
               'endpoints_with_source_differences': len(differences), 'catalog_matches': committed == generated}
    if args.report:
        Path(args.report).write_text(json.dumps({**summary, 'source_differences': differences}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary))
    if committed != generated:
        raise SystemExit('Catalog drift detected. Regenerate from the selected docs revision and review parameter/enum/required changes.')


if __name__ == '__main__':
    main()

"""Run two synthetic deliveries through PaperRoute without Telegram or iCloud."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from paperroute import run_delivery


def pdf_bytes(title):
    text = f'BT /F1 24 Tf 60 730 Td ({title}) Tj 0 -40 Td /F1 12 Tf (Synthetic PaperRoute demo. No real publication content.) Tj ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               f'<< /Length {len(text)} >>\nstream\n{text}\nendstream'.encode()]
    output = b'%PDF-1.4\n'
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output += f'{index} 0 obj\n'.encode() + obj + b'\nendobj\n'
    xref = len(output)
    output += f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode()
    output += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
    output += f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    return output


class DemoMessage:
    def __init__(self, number, name, content=None, fail=False):
        self.id = number
        self.content = content if content is not None else pdf_bytes(name)
        self.file = SimpleNamespace(name=name, size=len(self.content))
        self.fail = fail
        self.downloads = 0

    async def download_media(self, file):
        self.downloads += 1
        if self.fail:
            Path(file).write_bytes(b'%PDF-partial')
            raise ConnectionError('Synthetic interruption')
        Path(file).write_bytes(self.content)


class DemoClient:
    def __init__(self, messages):
        self.messages = messages

    async def get_entity(self, channel):
        return channel

    async def iter_messages(self, channel, limit):
        for message in self.messages[:limit]:
            yield message


def demo_config(root):
    return {'local_dir': root / 'reading', 'mirror_dir': root / 'cloud',
            'cleanup_confirmed': True, 'scan_limit': 500,
            'publications': [{'id': slug, 'channel': 'demo-source', 'patterns': [pattern]}
                             for slug, pattern in [('morning-paper', 'Morning*.pdf'),
                                                   ('weekend-review', 'Weekend*.pdf'),
                                                   ('research-journal', 'Research*.pdf')]]}


async def demo(root):
    config = demo_config(root)
    paper = DemoMessage(101, 'Morning-2026-01-01.pdf')
    magazine = DemoMessage(102, 'Weekend-2026-01.pdf')
    journal = DemoMessage(103, 'Research-2026-01.pdf')
    print('PAPERROUTE / offline demonstration\n')
    print('DAY 1 / Three publications arrive')
    await run_delivery(DemoClient([journal, magazine, paper]), config)
    print('\nDAY 2 / A new newspaper; monthly issues stay')
    replacement = DemoMessage(104, 'Morning-2026-01-02.pdf')
    await run_delivery(DemoClient([replacement, journal, magazine, paper]), config)
    print('\nREADING FOLDER / verified local mirror')
    files = sorted(config['mirror_dir'].glob('*/*.pdf'))
    for path in files:
        print(f'  {path.parent.name}/')
        print(f'    {path.name.split("-", 2)[2]}')
    assert len(files) == 3
    assert paper.downloads == magazine.downloads == journal.downloads == replacement.downloads == 1
    assert not any('Morning-2026-01-01' in p.name for p in files)
    print('\nPASS / Latest issues kept; previous newspaper removed.')
    print('Synthetic PDFs only. No network, account or iCloud access.')


if __name__ == '__main__':
    with TemporaryDirectory(prefix='paperroute-demo-') as directory:
        asyncio.run(demo(Path(directory).resolve()))

"""Bounded, non-persistent invoice reconciliation and dependency-free XLSX reports."""
import csv
import io
import re
import zipfile
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000
NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
HEADERS = ['account_id', 'month', 'currency', 'amount']


def parse_upload(upload, month, currency):
    if upload.size > MAX_BYTES:
        raise ValueError('Upload a CSV or XLSX file smaller than 2 MB.')
    raw = upload.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('Upload a file smaller than 2 MB.')
    try:
        if upload.name.lower().endswith('.csv'):
            rows = csv.reader(io.StringIO(raw.decode('utf-8-sig')))
        elif upload.name.lower().endswith('.xlsx'):
            rows = xlsx_rows(raw)
        else:
            raise ValueError('Use .csv or .xlsx with account_id, month, currency and amount columns.')
        rows = iter(rows)
        header = [str(v).strip().lower() for v in next(rows)]
        if header != HEADERS:
            raise ValueError('Use the downloaded template; columns must be account_id, month, currency, amount.')
        result = {}
        for number, row in enumerate(rows, 2):
            if number > MAX_ROWS + 1:
                raise ValueError('Upload at most 5,000 rows.')
            if not any(str(v).strip() for v in row):
                continue
            if len(row) != 4:
                raise ValueError(f'Row {number}: expected four columns.')
            account, period, unit, amount = [str(v).strip() for v in row]
            if not re.fullmatch(r'[0-9]{12}', account):
                raise ValueError(f'Row {number}: preserve the 12-digit account ID as text, including leading zeros.')
            if period != month.strftime('%Y-%m') or unit != currency:
                raise ValueError(f'Row {number}: month and currency must match the selected filters.')
            try:
                value = Decimal(amount)
                if not value.is_finite() or abs(value) > Decimal('1000000000000') or value.as_tuple().exponent < -8:
                    raise InvalidOperation
            except InvalidOperation:
                raise ValueError(f'Row {number}: enter a finite amount with up to eight decimal places.') from None
            if account in result:
                raise ValueError(f'Row {number}: duplicate account. Combine invoice lines before uploading.')
            result[account] = value
        if not result:
            raise ValueError('The upload has no account rows.')
        return result
    except (UnicodeError, csv.Error, StopIteration, zipfile.BadZipFile, ET.ParseError, KeyError, RuntimeError, NotImplementedError):
        raise ValueError('The file could not be read. Use an unencrypted CSV or XLSX template.') from None


def xlsx_rows(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        if len(infos) > 100 or sum(i.file_size for i in infos) > 10 * MAX_BYTES:
            raise ValueError('The workbook is too large after decompression.')
        def xml(name):
            # Normalize before checking declarations so alternate encodings cannot bypass it.
            data = archive.read(name).decode('utf-8-sig')
            if '<!DOCTYPE' in data.upper() or '<!ENTITY' in data.upper():
                raise ValueError('XML declarations are not supported.')
            return ET.fromstring(data)  # nosec B314: bounded UTF-8 XML; DTD/entity declarations rejected above.
        shared = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            shared = [''.join(n.itertext()) for n in xml('xl/sharedStrings.xml').findall(NS + 'si')]
        sheets = [i.filename for i in infos if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', i.filename)]
        if len(sheets) != 1:
            raise ValueError('Upload a workbook with exactly one worksheet.')
        for row in xml(sheets[0]).iter(NS + 'row'):
            values = ['', '', '', '']
            for cell in row.findall(NS + 'c'):
                if cell.find(NS + 'f') is not None:
                    raise ValueError('Replace spreadsheet formulas with values before uploading.')
                ref = cell.get('r', '')
                match = re.fullmatch(r'([A-D])[0-9]+', ref)
                if not match:
                    raise ValueError('The worksheet must contain only the four template columns.')
                value = cell.findtext(NS + 'v', '')
                if cell.get('t') == 's':
                    try:
                        value = shared[int(value)]
                    except (ValueError, IndexError):
                        raise ValueError('Invalid shared string in workbook.') from None
                elif cell.get('t') == 'inlineStr':
                    value = ''.join(cell.find(NS + 'is').itertext()) if cell.find(NS + 'is') is not None else ''
                values[ord(match[1]) - ord('A')] = value
            yield values


def reconcile(rows, uploaded):
    visible = {r['account_id']: r for r in rows}
    if set(uploaded) - visible.keys():
        raise ValueError('The upload includes accounts outside the selected customer, authorized scope or reporting history.')
    result = []
    for account, r in visible.items():
        invoice = uploaded.get(account)
        aws = r['current']['value']
        complete = r['current']['state'] == 'Complete'
        difference = invoice - aws if invoice is not None and aws is not None else None
        status = ('Not in upload' if invoice is None else 'AWS data incomplete' if not complete else
                  'Matched' if abs(difference) <= Decimal('.01') else 'Difference')
        result.append({'account_id': account, 'customer': r['customer'], 'name': r['name'], 'invoice': invoice,
                       'aws': aws, 'difference': difference, 'state': r['current']['state'], 'status': status, 'next_action': NEXT_ACTIONS[status]})
    return result


NEXT_ACTIONS = {
    'Matched': 'No amount difference above tolerance. Check invoice taxes and agreed commercial terms before approval.',
    'Difference': 'Review credits, taxes, reseller margin and adjustments with the billing owner. Positive difference means invoice exceeds AWS.',
    'AWS data incomplete': 'Wait for complete AWS collection or resolve the sync issue, then compare again. Do not approve from this provisional amount.',
    'Not in upload': 'Add this account to the invoice file, or confirm that it is intentionally excluded from this invoice.',
}


def reconciliation_summary(rows):
    """Totals compare the identical uploaded account set; unavailable AWS never becomes zero."""
    submitted = [row for row in rows if row['invoice'] is not None]
    complete = all(row['state'] == 'Complete' for row in submitted)
    invoice_total = sum((row['invoice'] for row in submitted), Decimal(0))
    known = [row['aws'] for row in submitted if row['aws'] is not None]
    aws_total = sum(known, Decimal(0)) if len(known) == len(submitted) else None
    return {
        'invoice_total': invoice_total, 'uploaded_count': len(submitted),
        'aws_total': aws_total, 'totals_complete': complete,
        'difference_total': invoice_total - aws_total if complete and aws_total is not None else None,
        'matched': sum(row['status'] == 'Matched' for row in rows),
        'differences': sum(row['status'] == 'Difference' for row in rows),
        'incomplete': sum(row['status'] == 'AWS data incomplete' for row in rows),
        'not_uploaded': sum(row['status'] == 'Not in upload' for row in rows),
    }


def result_sheets(rows, summary, customer, month, currency, generated_at):
    overview = [['Invoice reconciliation', 'Value'],
        ['Customer', customer.name], ['Billing month', month.strftime('%Y-%m')], ['Currency', currency],
        ['Generated at (UTC)', generated_at.isoformat()], ['AWS metric', 'Unblended cost'],
        ['Uploaded accounts', summary['uploaded_count']], ['Uploaded invoice total', summary['invoice_total']],
        ['AWS total for uploaded accounts', summary['aws_total']], ['Confirmed invoice minus AWS total', summary['difference_total']],
        ['AWS total status', 'Complete' if summary['totals_complete'] else 'Provisional / incomplete'],
        ['Matched accounts', summary['matched']], ['Accounts with differences', summary['differences']],
        ['Uploaded accounts with incomplete AWS data', summary['incomplete']], ['Authorized accounts not in upload', summary['not_uploaded']],
        ['Match tolerance', Decimal('0.01')],
        ['Interpretation', 'Totals cover uploaded accounts only. Blank totals are unavailable, not zero. Differences use invoice minus AWS.'],
        ['Approval', 'This report is a comparison, not invoice approval. Confirm taxes, margins, credits and agreed commercial terms.']]
    details = [['AWS account ID', 'Account', 'Currency', 'Invoice', 'AWS unblended', 'Invoice minus AWS', 'AWS coverage', 'Result', 'Next action']]
    for row in rows:
        details.append([row['account_id'], row['name'], currency, row['invoice'], row['aws'], row['difference'], row['state'], row['status'], NEXT_ACTIONS[row['status']]])
    return [('Reconciliation summary', overview), ('Reconciliation accounts', details)]


def workbook(sheets):
    """Create portable XLSX with typed values, no executable formulas or external links."""
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' + ''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, len(sheets)+1)) + '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>')
        archive.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr('xl/workbook.xml', '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>' + ''.join(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>' for i, (name, _) in enumerate(sheets, 1)) + '</sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + ''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(sheets)+1)) + '<Relationship Id="styles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        archive.writestr('xl/styles.xml', '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF17365D"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment wrapText="1" vertical="top"/></xf><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment wrapText="1" vertical="center"/></xf><xf numFmtId="4" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
        for index, (name, rows) in enumerate(sheets, 1):
            summary = name == "Executive summary"
            widths = [30, 26, 24, 20, 20, 20, 60] if summary else [30, 24, 28, 22, 22, 22, 65] if name == "Service changes" else [30, 34, 22, 16, 20, 20, 20, 20, 18, 24, 24, 45]
            if name == 'Reconciliation summary':
                widths = [48, 95]
            elif name == 'Reconciliation accounts':
                widths = [22, 36, 14, 20, 20, 22, 26, 26, 85]
            columns = "".join(f'<col min="{i}" max="{i}" width="{width}" customWidth="1"/>' for i, width in enumerate(widths, 1))
            freeze = 7 if summary else 1
            cells = []
            for number, row in enumerate(rows, 1):
                content = []
                for col, value in enumerate(row):
                    ref = f'{chr(65+col)}{number}'
                    if value is None:
                        content.append(f'<c r="{ref}"/>')
                    elif isinstance(value, (int, Decimal)):
                        content.append(f'<c r="{ref}" s="{2 if isinstance(value, Decimal) else 0}"><v>{value}</v></c>')
                    else:
                        clean = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(value))
                        content.append(f'<c r="{ref}" s="{1 if number == 1 or (summary and number == 7) else 0}" t="inlineStr"><is><t xml:space="preserve">{escape(clean)}</t></is></c>')
                height = ' ht="60" customHeight="1"' if (summary and number in (5, 6)) or name.startswith('Reconciliation') else ' ht="32" customHeight="1"'
                cells.append(f'<row r="{number}"{height}>{"".join(content)}</row>')
            archive.writestr(f'xl/worksheets/sheet{index}.xml', f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"><pane ySplit="{freeze}" topLeftCell="A{freeze+1}" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols>{columns}</cols><sheetData>' + ''.join(cells) + '</sheetData></worksheet>')
    return output.getvalue()

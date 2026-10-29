"""真实版式坐标的合成 PDF 回归，不保存或嵌入真实论文全文。"""
import io
import unittest

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from highscore_literature.pdf import validate_pdf

TITLE = 'Synthetic article for publication footer verification'
DOI = '10.1234/synthetic.2026.182374'


def footer_pdf(doi=DOI, *, y=89.4614, height=793.701, references=False, title=TITLE):
    writer = PdfWriter()
    page = writer.add_blank_page(width=595.276, height=height)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                             NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({
        NameObject('/F1'): writer._add_object(font)})})
    content = f'BT /F1 12 Tf 30 {height - 40} Td ({title}) Tj ET\n'
    content += f'BT /F1 12 Tf 30 {height - 80} Td (Abstract) Tj ET\n'
    if references:
        content += f'BT /F1 12 Tf 30 180 Td (References) Tj ET\n'
    # 与现场 PDF 同类的 CTM 缩放；出版 DOI 位于距页底 89.4614 pt。
    content += f'q 7.173 0 0 7.173 37.5874 {y} cm BT /F1 1 Tf (https://doi.org/{doi}) Tj ET Q'
    stream = DecodedStreamObject()
    stream.set_data(content.encode('ascii'))
    page[NameObject('/Contents')] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class LiteraturePdfTests(unittest.TestCase):
    def verified(self, data):
        try:
            return validate_pdf(data, TITLE, DOI)
        except ValueError as exc:
            self.fail('独立出版 DOI 页脚被误拒绝：' + str(exc))

    def test_scaled_publication_footer_above_72_points_is_verified(self):
        self.assertEqual(self.verified(footer_pdf())['pages'], 1)

    def test_footer_band_is_relative_to_page_and_capped(self):
        self.assertEqual(self.verified(footer_pdf(y=100, height=1000))['pages'], 1)
        for y, height in ((97, 800), (109, 1600), (121, 1000)):
            with self.subTest(y=y, height=height), self.assertRaises(ValueError):
                validate_pdf(footer_pdf(y=y, height=height), TITLE, DOI)

    def test_wrong_or_prefix_doi_cannot_pass_publication_footer(self):
        for doi in (DOI + '2', DOI + '.s001', '10.1234/another'):
            with self.subTest(doi=doi), self.assertRaises(ValueError):
                validate_pdf(footer_pdf(doi), TITLE, DOI)

    def test_reference_footer_cannot_establish_article_identity(self):
        with self.assertRaises(ValueError):
            validate_pdf(footer_pdf(references=True), TITLE, DOI)

    def test_correct_doi_still_requires_exact_article_title(self):
        with self.assertRaises(ValueError):
            validate_pdf(footer_pdf(title='A different article'), TITLE, DOI)

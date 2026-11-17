import unittest
from fastapi import HTTPException
from pydantic import ValidationError
from app.process_rules import FormField, NodeConfig, validate_answers, safe_link


class ProcessFormTests(unittest.TestCase):
    def test_required_number_accepts_zero_but_not_boolean(self):
        fields=[dict(id='n',label='数量',type='number',required=True)]
        validate_answers(fields,{'n':0},True,set(),set())
        for value in (None,True,float('inf')):
            with self.assertRaises(HTTPException):
                validate_answers(fields,{'n':value},True,set(),set())

    def test_draft_allows_missing_required_but_rejects_unknown_fields(self):
        fields=[dict(id='text',label='说明',type='text',required=True)]
        validate_answers(fields,{},False,set(),set())
        with self.assertRaises(HTTPException):
            validate_answers(fields,{},True,set(),set())
        with self.assertRaises(HTTPException):
            validate_answers(fields,{'admin':True},False,set(),set())

    def test_select_date_and_multiselect_validation(self):
        for field,value in [(dict(id='v',label='选项',type='select',options=['A']),'B'),
                            (dict(id='v',label='日期',type='date'),'2026-02-30'),
                            (dict(id='v',label='多选',type='multiselect',options=['A']),['A','A'])]:
            with self.assertRaises(HTTPException):
                validate_answers([field],{'v':value},True,set(),set())

    def test_attachments_are_scoped_and_links_do_not_contain_credentials(self):
        fields=[dict(id='files',label='附件',type='attachment',required=True)]
        with self.assertRaises(HTTPException):
            validate_answers(fields,{'files':[dict(kind='file',id='other-task-file',title='资料')]},True,set(),{'own-file'})
        for url in ('javascript:alert(1)','https://user:password@example.com/x','https://exa mple.com'):
            with self.assertRaises(HTTPException): safe_link(url)
        validate_answers(fields,{'files':[dict(kind='link',url='https://example.com/report',title='报告',version='v2')]},True,set(),set())

    def test_form_definitions_reject_duplicates_and_empty_choices(self):
        with self.assertRaises(ValidationError):
            FormField(id='choice',label='选择',type='select',options=[])
        with self.assertRaises(ValidationError):
            NodeConfig(expected_version=0,owner_id='00000000-0000-0000-0000-000000000001',
                       reviewer_ids=['00000000-0000-0000-0000-000000000001'],
                       fields=[dict(id='same',label='A',type='text'),dict(id='same',label='B',type='text')])

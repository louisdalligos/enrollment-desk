from unittest.mock import Mock
import json
import pytest
from enrollment_desk.column_mapping import BedrockColumnMapper, MappingUnavailable, validate_mapping, preview
from enrollment_desk.roster_parser import parse_roster


def test_reviewed_unfamiliar_headers_parse_without_changing_values():
    data = b'Payroll reference,Given appellation,Family appellation\nSYN-1,Ana,Santos\n'
    selected = preview('test.csv', data)
    assert selected['header_row'] == 0
    mapping = {'0': 'employee_id', '1': 'first_name', '2': 'last_name'}
    result = parse_roster('test.csv', data, header_row=0, column_map=mapping)
    assert result.members[0].employee_id == 'SYN-1'
    assert result.members[0].first_name == 'Ana'


@pytest.mark.parametrize('mapping', [
    {'0': 'first_name', '1': 'first_name'},
    {'0': 'full_name', '1': 'last_name'},
    {'0': 'coverage_status', '1': None},
    {'0': None, '1': None},
    {'0': 'first_name'},
])
def test_invalid_mapping_rejected(mapping):
    with pytest.raises(ValueError):
        validate_mapping(['A', 'B'], mapping)


def test_bedrock_receives_labels_only_and_output_is_validated():
    client = Mock()
    client.converse.return_value = {'output': {'message': {'content': [{'toolUse': {
        'name': 'propose_column_mapping', 'input': {'assignments': [
            {'column': 0, 'field': 'employee_id', 'reason': 'Payroll identifier'}]}}}]}}}
    result = BedrockColumnMapper(client).suggest(['Payroll reference'])
    assert result['mapping'] == {'0': 'employee_id'}
    payload = json.loads(client.converse.call_args.kwargs['messages'][0]['content'][0]['text'])
    assert payload == {'unfamiliar_columns': [{'column': 0, 'header': 'Payroll reference'}], 'existing_fields': []}
    client.converse.return_value['output']['message']['content'][0]['toolUse']['input']['assignments'][0]['field'] = 'active'
    with pytest.raises(MappingUnavailable):
        BedrockColumnMapper(client).suggest(['Payroll reference'])


def test_model_and_region_restricted():
    with pytest.raises(ValueError):
        BedrockColumnMapper(region='ap-southeast-1')
    with pytest.raises(ValueError):
        BedrockColumnMapper(model_id='arbitrary')

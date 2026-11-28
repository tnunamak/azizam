from gateway import capability_ids
import yaml


def test_new_presentation_is_data(tmp_path):
    path = tmp_path / 'words.yml'
    path.write_text(yaml.safe_dump({'presentation': {'tabs': {'new.run': 'image'},
        'category_choices': [{'id': 'new.run', 'label': 'New activity'}],
        'text_image_intents': ['new.run'], 'text_image_task': 'new-task'}}))
    result = capability_ids.load_ui_presentation(path)
    assert result['tabs']['new.run'] == 'image'
    assert result['category_choices'][0]['label'] == 'New activity'
    assert 'new.run' in result['text_image_intents']

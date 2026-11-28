def test_status_examples_use_selected_catalog():
    import firehose_render
    rows = [{'id':'novel-model','intent':'image.generate'}]
    html = firehose_render._render_quick_test(rows)
    assert 'novel-model' in html
    assert 'krea2-turbo' not in html

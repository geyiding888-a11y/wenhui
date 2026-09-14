from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from streamlit.testing.v1 import AppTest
from wenhui.store import Store


def test_pages_and_summary():
    with TemporaryDirectory() as tmp:
        with patch('wenhui.store.Store', return_value=Store(Path(tmp)/'test.db')), patch('wenhui.config.OUTPUT_DIR', Path(tmp)), patch('wenhui.config.get_api_key', return_value=None):
            app = AppTest.from_file(Path(__file__).resolve().parents[1] / 'src/wenhui/app.py', default_timeout=30).run()
            assert not app.exception
            assert any(b.label == '开始汇总' for b in app.button)
            for origin in ['智能助手', '我的资料库', '汇总归档', '设置与状态']:
                app.radio(key='section').set_value(origin).run()
                next(b for b in app.button if b.label == '查看全部来源 →').click().run()
                assert not app.exception, origin
                assert app.radio(key='section').value == '我的资料库'
                assert any(h.value == '我的资料库' for h in app.subheader)
            app.radio(key='section').set_value('智能助手').run()
            next(b for b in app.button if b.label == '开始汇总').click().run()
            assert not app.exception
            result = app.session_state['result']
            assert len(result.records) > 0
            assert result.summary_path.exists()
            assert result.issues_path.exists()
            if result.uncertain_columns:
                next(b for b in app.button if b.label == '保存我的确认').click().run()
                assert not app.exception
                assert any('重新生成总表' in item.value for item in app.success)
            for page in ['我的资料库', '汇总归档', '设置与状态']:
                app.radio(key='section').set_value(page).run()
                assert not app.exception, page



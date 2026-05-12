"""
tests/test_app.py — Testes unitários do backend Flask
======================================================
Execução:
    cd "C:\\Users\\Administrator\\Documents\\venda feita\\projeto bet"
    C:\\PythonPortable\\python312\\python.exe -m pytest tests/ -v
"""

import json
import os
import sys
import tempfile
import pytest

# Garante que o diretório do projeto está no path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
def cliente():
    """Cria cliente de teste Flask com dados fictícios."""
    # Importa app apenas depois de configurar PYTHONPATH
    import app as flask_app
    flask_app.app.config['TESTING'] = True
    with flask_app.app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# Testes de validação (sem servidor)
# ---------------------------------------------------------------------------

class TestValidacoes:

    def test_cnpj_valido_14_digitos(self):
        from app import _validar_cnpj_formato
        assert _validar_cnpj_formato('12345678000100') is True

    def test_cnpj_invalido_menos_digitos(self):
        from app import _validar_cnpj_formato
        assert _validar_cnpj_formato('1234567800') is False

    def test_cnpj_valido_com_mascara(self):
        """Máscara deve ser stripped antes da validação."""
        from app import _validar_cnpj_formato
        assert _validar_cnpj_formato('12.345.678/0001-00') is True

    def test_url_valida_https(self):
        from app import _validar_url_segura
        assert _validar_url_segura('https://example.com') is True

    def test_url_valida_http(self):
        from app import _validar_url_segura
        assert _validar_url_segura('http://bet.com.br') is True

    def test_url_invalida_sem_scheme(self):
        from app import _validar_url_segura
        assert _validar_url_segura('example.com') is False

    def test_url_invalida_ftp(self):
        from app import _validar_url_segura
        assert _validar_url_segura('ftp://files.example.com') is False

    def test_url_vazia_retorna_false(self):
        from app import _validar_url_segura
        assert _validar_url_segura('') is False


# ---------------------------------------------------------------------------
# Testes do display_afiliados
# ---------------------------------------------------------------------------

class TestDisplayAfiliados:

    def test_encontrado_completo_retorna_sim(self):
        from app import _display_afiliados
        assert _display_afiliados('encontrado_completo') == 'sim'

    def test_encontrado_url_retorna_sim(self):
        from app import _display_afiliados
        assert _display_afiliados('encontrado_url') == 'sim'

    def test_nao_encontrado_retorna_nao(self):
        from app import _display_afiliados
        assert _display_afiliados('nao_encontrado') == 'nao'

    def test_bloqueado_robots_retorna_nao(self):
        from app import _display_afiliados
        assert _display_afiliados('bloqueado_robots') == 'nao'

    def test_desconhecido_retorna_nao_encontrado(self):
        from app import _display_afiliados
        assert _display_afiliados('') == 'nao_encontrado'

    def test_erro_conexao_retorna_nao_encontrado(self):
        from app import _display_afiliados
        assert _display_afiliados('erro_conexao') == 'nao_encontrado'


# ---------------------------------------------------------------------------
# Testes dos helpers de filtro
# ---------------------------------------------------------------------------

class TestAplicarFiltros:

    def _registros(self):
        return [
            {'marca': 'BetA',   'uf': 'SP', 'status': 'encontrado',
             'email_contato': 'contato@beta.bet.br',          # tem email → filtro com_email
             '_afiliados_display': 'sim', 'porte_empresa': 'PEQUENO PORTE',
             'municipio': 'São Paulo', 'data_coleta': '2025-04-01',
             '_url_health_status': 'ok', '_url_inativa': False},
            {'marca': 'BetB',   'uf': 'RJ', 'status': 'nao_encontrado',
             'email_contato': '',                              # sem email
             '_afiliados_display': 'nao', 'porte_empresa': 'MÉDIO PORTE',
             'municipio': 'Rio de Janeiro', 'data_coleta': '2025-03-01',
             '_url_health_status': 'erro_http', '_url_inativa': True},
            {'marca': 'GambC',  'uf': 'SP', 'status': 'erro_conexao',
             '_afiliados_display': 'nao_encontrado', 'porte_empresa': '',
             'municipio': 'Campinas', 'data_coleta': '2025-04-15',
             '_url_health_status': 'desconhecido', '_url_inativa': False},
        ]

    def test_filtro_marca(self):
        from app import _aplicar_filtros_query
        result = _aplicar_filtros_query(self._registros(), {'marca': 'bet'})
        assert len(result) == 2

    def test_filtro_uf(self):
        from app import _aplicar_filtros_query
        result = _aplicar_filtros_query(self._registros(), {'uf': 'SP'})
        assert len(result) == 2

    def test_filtro_status_com_email(self):
        from app import _aplicar_filtros_query
        result = _aplicar_filtros_query(self._registros(), {'status': 'com_email'})
        assert len(result) == 1
        assert result[0]['marca'] == 'BetA'

    def test_filtro_afiliados_com(self):
        from app import _aplicar_filtros_query
        result = _aplicar_filtros_query(self._registros(), {'afiliados': 'com'})
        assert len(result) == 1
        assert result[0]['marca'] == 'BetA'

    def test_filtro_saude_url_ok(self):
        from app import _aplicar_filtros_query
        result = _aplicar_filtros_query(self._registros(), {'saude_url': 'ok'})
        assert len(result) == 1
        assert result[0]['marca'] == 'BetA'

    def test_filtro_saude_url_online(self):
        """'online' abrange ok + redirect + bloqueado (todos acessíveis por usuários reais)."""
        from app import _aplicar_filtros_query
        result = _aplicar_filtros_query(self._registros(), {'saude_url': 'online'})
        # _registros tem BetA(ok) e GambC(desconhecido) e BetB(erro_http/inativa)
        assert len(result) == 1
        assert result[0]['marca'] == 'BetA'

    def test_filtro_data_inicio(self):
        from app import _aplicar_filtros_query
        result = _aplicar_filtros_query(self._registros(), {'data_inicio': '2025-04-01'})
        assert len(result) == 2

    def test_sem_filtros_retorna_tudo(self):
        from app import _aplicar_filtros_query
        result = _aplicar_filtros_query(self._registros(), {})
        assert len(result) == 3


# ---------------------------------------------------------------------------
# Testes de endpoints HTTP
# ---------------------------------------------------------------------------

class TestEndpoints:

    def test_index_retorna_200(self, cliente):
        resp = cliente.get('/')
        assert resp.status_code == 200

    def test_api_stats_retorna_json(self, cliente):
        resp = cliente.get('/api/stats')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert 'total' in data
        assert 'com_email' in data
        assert 'playwright_disponivel' in data

    def test_api_dados_retorna_lista(self, cliente):
        resp = cliente.get('/api/dados')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert isinstance(data, list)

    def test_api_dados_paginado(self, cliente):
        resp = cliente.get('/api/dados?pagina=1&limite=5')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert 'dados' in data
        assert 'total' in data
        assert 'total_paginas' in data
        assert isinstance(data['dados'], list)

    def test_api_snapshots_retorna_lista(self, cliente):
        resp = cliente.get('/api/snapshots')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert isinstance(data, list)

    def test_api_duplicatas_retorna_lista(self, cliente):
        resp = cliente.get('/api/duplicatas')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert isinstance(data, list)

    def test_api_audit_log_retorna_lista(self, cliente):
        resp = cliente.get('/api/audit-log')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert isinstance(data, list)

    def test_api_sistema_retorna_versoes(self, cliente):
        resp = cliente.get('/api/sistema')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert 'python' in data
        assert 'flask' in data

    def test_api_editar_cnpj_invalido(self, cliente):
        resp = cliente.post('/api/editar', json={
            'cnpj': '123',  # inválido
            'campo': 'email_contato',
            'valor': 'test@test.com',
        })
        assert resp.status_code == 400

    def test_api_editar_campo_invalido(self, cliente):
        resp = cliente.post('/api/editar', json={
            'cnpj': '12345678000100',
            'campo': 'campo_inexistente',
            'valor': 'valor',
        })
        assert resp.status_code == 400

    def test_api_editar_email_invalido(self, cliente):
        resp = cliente.post('/api/editar', json={
            'cnpj': '12345678000100',
            'campo': 'email_contato',
            'valor': 'nao-e-um-email',
        })
        assert resp.status_code == 400

    def test_api_editar_url_invalida(self, cliente):
        resp = cliente.post('/api/editar', json={
            'cnpj': '12345678000100',
            'campo': 'url',
            'valor': 'sem-http.com',
        })
        assert resp.status_code == 400

    def test_auditoria_page_retorna_200(self, cliente):
        resp = cliente.get('/auditoria')
        assert resp.status_code == 200

    def test_api_notificacoes_config_get(self, cliente):
        resp = cliente.get('/api/notificacoes/config')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        # Pode retornar config padrão ou erro se módulo não encontrado
        assert isinstance(data, dict)


# ---------------------------------------------------------------------------
# Testes do stats_snapshot
# ---------------------------------------------------------------------------

class TestStatsSnapshot:

    def test_calcular_stats_basico(self):
        import stats_snapshot
        dados = [
            {'status': 'encontrado',     '_afiliados_display': 'sim', '_editado_manualmente': True,  '_url_health_status': 'ok',           '_url_inativa': False},
            {'status': 'nao_encontrado', '_afiliados_display': 'nao', '_editado_manualmente': False, '_url_health_status': 'erro_conexao',  '_url_inativa': True},
            {'status': 'erro_conexao',   '_afiliados_display': 'nao_encontrado', '_editado_manualmente': False, '_url_health_status': 'desconhecido', '_url_inativa': False},
        ]
        s = stats_snapshot._calcular_stats(dados)
        assert s['total']         == 3
        assert s['com_email']     == 1
        assert s['sem_email']     == 1
        assert s['com_afiliados'] == 1
        assert s['editados']      == 1
        assert s['urls_ativas']   == 1
        assert s['urls_inativas'] == 1

    def test_snapshot_atual_tem_data(self):
        import stats_snapshot
        snap = stats_snapshot.snapshot_atual([])
        assert 'data' in snap
        assert 'total' in snap

    def test_ler_snapshots_sem_arquivo(self, tmp_path, monkeypatch):
        import stats_snapshot
        monkeypatch.setattr(stats_snapshot, 'ARQUIVO_SNAPSHOTS', tmp_path / 'inexistente.json')
        result = stats_snapshot.ler_snapshots()
        assert result == []

    def test_registrar_e_ler(self, tmp_path, monkeypatch):
        import stats_snapshot
        monkeypatch.setattr(stats_snapshot, 'ARQUIVO_SNAPSHOTS', tmp_path / 'snaps.json')
        dados = [{'status': 'encontrado', '_afiliados_display': 'sim',
                  '_editado_manualmente': False, '_url_health_status': 'ok', '_url_inativa': False}]
        gravado = stats_snapshot.registrar_snapshot_se_necessario(dados)
        assert gravado is True
        snaps = stats_snapshot.ler_snapshots()
        assert len(snaps) == 1
        assert snaps[0]['total'] == 1

    def test_nao_duplica_mesmo_dia(self, tmp_path, monkeypatch):
        import stats_snapshot
        monkeypatch.setattr(stats_snapshot, 'ARQUIVO_SNAPSHOTS', tmp_path / 'snaps2.json')
        dados = []
        stats_snapshot.registrar_snapshot_se_necessario(dados)
        segunda = stats_snapshot.registrar_snapshot_se_necessario(dados)
        assert segunda is False   # já tinha snapshot de hoje
        assert len(stats_snapshot.ler_snapshots()) == 1


# ---------------------------------------------------------------------------
# Testes do notificacoes
# ---------------------------------------------------------------------------

class TestNotificacoes:

    def test_config_padrao(self, tmp_path, monkeypatch):
        import notificacoes
        monkeypatch.setattr(notificacoes, 'ARQUIVO_CONFIG', tmp_path / 'cfg.json')
        cfg = notificacoes.ler_config()
        assert cfg['habilitado'] is False
        assert cfg['webhook_url'] == ''

    def test_salvar_e_ler_config(self, tmp_path, monkeypatch):
        import notificacoes
        monkeypatch.setattr(notificacoes, 'ARQUIVO_CONFIG', tmp_path / 'cfg2.json')
        notificacoes.salvar_config({'habilitado': True, 'webhook_url': 'https://hook.example.com'})
        cfg = notificacoes.ler_config()
        assert cfg['habilitado'] is True
        assert cfg['webhook_url'] == 'https://hook.example.com'

    def test_disparar_teste_sem_url_retorna_false(self, tmp_path, monkeypatch):
        import notificacoes
        monkeypatch.setattr(notificacoes, 'ARQUIVO_CONFIG', tmp_path / 'cfg3.json')
        ok, msg = notificacoes.disparar_teste()
        assert ok is False
        assert 'URL' in msg or 'url' in msg.lower() or 'configurad' in msg.lower()

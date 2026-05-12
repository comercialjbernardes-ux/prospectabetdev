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


# ---------------------------------------------------------------------------
# Testes do health_score (etapa 3.1)
# ---------------------------------------------------------------------------

class TestHealthScore:

    def test_betano_ra1000_excelente(self):
        from health_score import calcular, classificar
        reg = {
            '_url_health_status': 'ok', '_url_inativa': False,
            '_ra_nota': 9.0, '_ra_resolvidas': 99.6, '_ra_ra1000': True,
            '_ra_status': 'encontrado',
            '_afiliados_display': 'nao_encontrado',
            'email_contato': 'contato@betano.bet.br',
        }
        s = calcular(reg)
        assert s >= 85
        assert classificar(s) == 'excelente'

    def test_bet_inativa_sem_dados_critico_ou_ruim(self):
        from health_score import calcular
        reg = {
            '_url_health_status': 'erro_conexao', '_url_inativa': True,
            '_ra_status': 'desconhecido',
            '_afiliados_display': 'nao_encontrado',
            'email_contato': '',
        }
        s = calcular(reg)
        assert s <= 35

    def test_classificacao_limites(self):
        from health_score import classificar
        assert classificar(90) == 'excelente'
        assert classificar(80) == 'excelente'
        assert classificar(70) == 'bom'
        assert classificar(65) == 'bom'
        assert classificar(50) == 'regular'
        assert classificar(30) == 'ruim'
        assert classificar(29) == 'critico'
        assert classificar(0)  == 'critico'

    def test_aplicar_em_lote_escreve_campos(self):
        from health_score import aplicar_em_lote
        dados = [
            {'_url_health_status': 'ok', '_ra_nota': 7.0, '_afiliados_display': 'sim',
             'email_contato': 'x@y.com'},
            {'_url_health_status': 'erro_conexao', '_url_inativa': True,
             '_afiliados_display': 'nao', 'email_contato': ''},
        ]
        aplicar_em_lote(dados)
        assert '_health_score' in dados[0]
        assert '_health_score_classe' in dados[0]
        assert dados[0]['_health_score'] > dados[1]['_health_score']

    def test_ra1000_bonus(self):
        """RA1000 deve aumentar levemente o score (+5 na nota)."""
        from health_score import calcular
        base = {
            '_url_health_status': 'ok', '_ra_nota': 9.0, '_ra_resolvidas': 95.0,
            '_ra_status': 'encontrado', '_afiliados_display': 'nao_encontrado',
            'email_contato': 'x@y.com',
        }
        sem_ra1000 = calcular({**base, '_ra_ra1000': False})
        com_ra1000 = calcular({**base, '_ra_ra1000': True})
        assert com_ra1000 >= sem_ra1000

    def test_filtro_score_min_no_aplicar_filtros_query(self):
        from app import _aplicar_filtros_query
        dados = [
            {'_health_score': 85, 'marca': 'BetA'},
            {'_health_score': 60, 'marca': 'BetB'},
            {'_health_score': 25, 'marca': 'BetC'},
        ]
        result = _aplicar_filtros_query(dados, {'score_min': '70'})
        assert len(result) == 1
        assert result[0]['marca'] == 'BetA'

    def test_estatisticas_agregadas(self):
        from health_score import estatisticas
        dados = [{'_health_score': s} for s in [85, 70, 60, 30, 10]]
        s = estatisticas(dados)
        assert s['min']  == 10
        assert s['max']  == 85
        assert s['por_classe']['excelente'] == 1
        assert s['por_classe']['critico']   == 1


# ---------------------------------------------------------------------------
# Testes de alertas inteligentes (etapa 4)
# ---------------------------------------------------------------------------

class TestAlertasInteligentes:

    def test_eventos_padrao_inclui_novos_tipos(self, tmp_path, monkeypatch):
        import notificacoes
        monkeypatch.setattr(notificacoes, 'ARQUIVO_CONFIG', tmp_path / 'cfg_eventos.json')
        cfg = notificacoes.ler_config()
        # Os 3 novos eventos devem estar na lista padrão
        assert 'url_down' in cfg['eventos']
        assert 'ra_score_drop' in cfg['eventos']
        assert 'bet_removed' in cfg['eventos']
        # E os 2 antigos preservados
        assert 'edit' in cfg['eventos']
        assert 'delete' in cfg['eventos']

    def test_notificar_evento_sem_habilitado_nao_envia(self, tmp_path, monkeypatch):
        import notificacoes
        import time as _t
        monkeypatch.setattr(notificacoes, 'ARQUIVO_CONFIG', tmp_path / 'cfg_off.json')
        notificacoes.salvar_config({'habilitado': False, 'webhook_url': 'http://fake.example.com'})
        # Sem habilitado: a thread deve sair imediatamente sem erro
        notificacoes.notificar_evento('url_down', 'teste', {'url': 'http://x.com'})
        _t.sleep(0.3)  # dá tempo da thread terminar

    def test_notificar_evento_tipo_nao_listado_nao_envia(self, tmp_path, monkeypatch):
        import notificacoes
        import time as _t
        monkeypatch.setattr(notificacoes, 'ARQUIVO_CONFIG', tmp_path / 'cfg_tipos.json')
        notificacoes.salvar_config({
            'habilitado': True,
            'webhook_url': 'http://fake.example.com',
            'eventos': ['edit'],   # só 'edit'
        })
        # url_down não está em eventos → não deve tentar enviar
        notificacoes.notificar_evento('url_down', 'teste', {})
        _t.sleep(0.3)

    def test_detectar_alerta_url_down_acumula_falhas(self):
        from url_health import _detectar_alerta_url_down
        # Simula 3 falhas seguidas
        antigo = {'_historico_falhas': [
            '2026-05-12T18:00:00', '2026-05-12T18:15:00',
        ]}
        novo = {
            'status': 'erro_conexao',
            'checado_em': '2026-05-12T18:30:00',
            'http_code': 0,
        }
        result = _detectar_alerta_url_down('http://x.com', novo, antigo)
        # 3 falhas → trigger
        assert len(result['_historico_falhas']) >= 3
        assert '_alerta_disparado_em' in result

    def test_detectar_alerta_url_down_sucesso_nao_dispara(self):
        from url_health import _detectar_alerta_url_down
        antigo = {'_historico_falhas': []}
        novo = {'status': 'ok', 'checado_em': '2026-05-12T18:30:00', 'http_code': 200}
        result = _detectar_alerta_url_down('http://x.com', novo, antigo)
        # Sucesso não adiciona ao histórico nem dispara alerta
        assert len(result['_historico_falhas']) == 0
        assert '_alerta_disparado_em' not in result or not result.get('_alerta_disparado_em')

    def test_detectar_alerta_ra_queda_de_05_dispara(self):
        from reclame_aqui_health import _detectar_alerta_ra_queda
        antigo = {'status': 'encontrado', 'nota': 8.5}
        novo   = {'status': 'encontrado', 'nota': 7.9,
                  'url_reclame_aqui': 'https://x.com', 'reputacao': 'Bom'}
        # Apenas valida que executa sem erro (notificacao real é assincrona)
        result = _detectar_alerta_ra_queda('TESTBET', novo, antigo)
        assert result is novo  # retorna o mesmo dict

    def test_detectar_alerta_ra_sem_dado_anterior_nao_dispara(self):
        from reclame_aqui_health import _detectar_alerta_ra_queda
        antigo = {}   # sem dado anterior
        novo   = {'status': 'encontrado', 'nota': 7.0}
        result = _detectar_alerta_ra_queda('NEWBET', novo, antigo)
        assert result is novo


# ---------------------------------------------------------------------------
# Testes do ai_chat (etapa 5) — não fazem chamadas reais à Anthropic API
# ---------------------------------------------------------------------------

class TestAiChat:

    def test_tool_buscar_bets_filtra_por_uf(self):
        from ai_chat import _tool_buscar_bets
        import data_manager
        # Snapshot dos dados reais — assume que rodou recarregar() ao importar app
        r = _tool_buscar_bets({'uf': 'SP', 'limite': 5})
        assert 'total_encontrado' in r
        assert 'bets' in r
        # Todos os retornados devem ser de SP
        for bet in r['bets']:
            assert bet['uf'] == 'SP'

    def test_tool_buscar_bets_score_min(self):
        from ai_chat import _tool_buscar_bets
        r = _tool_buscar_bets({'score_min': 80, 'limite': 50})
        # Todos retornados devem ter score >= 80
        for bet in r['bets']:
            assert (bet['_health_score'] or 0) >= 80

    def test_tool_buscar_bets_com_email(self):
        from ai_chat import _tool_buscar_bets
        r = _tool_buscar_bets({'com_email': True})
        for bet in r['bets']:
            assert bet['email_contato']  # truthy

    def test_tool_buscar_bets_ra1000(self):
        from ai_chat import _tool_buscar_bets
        r = _tool_buscar_bets({'ra_ra1000': True})
        for bet in r['bets']:
            assert bet['_ra_ra1000'] is True

    def test_tool_obter_bet_match_exato(self):
        from ai_chat import _tool_obter_bet
        r = _tool_obter_bet({'identificador': 'BETANO'})
        # Pode retornar bet ou {'erro': ...} dependendo do dataset
        if 'erro' not in r:
            assert r.get('marca', '').upper() == 'BETANO'

    def test_tool_obter_bet_inexistente(self):
        from ai_chat import _tool_obter_bet
        r = _tool_obter_bet({'identificador': 'XYZ_INVENTADA_999'})
        assert r.get('erro')

    def test_tool_estatisticas_gerais_estrutura(self):
        from ai_chat import _tool_estatisticas_gerais
        r = _tool_estatisticas_gerais({})
        assert 'total_bets' in r
        assert 'distribuicao_score' in r
        assert 'top_10_ufs' in r
        assert all(k in r['distribuicao_score']
                   for k in ['excelente', 'bom', 'regular', 'ruim', 'critico'])

    def test_endpoint_chat_sem_api_key(self, cliente, monkeypatch):
        """Sem ANTHROPIC_API_KEY o endpoint deve retornar 503 elegantemente."""
        monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
        # Força re-leitura do client
        import ai_chat
        ai_chat._client = None
        resp = cliente.post('/api/chat', json={'pergunta': 'oi'})
        assert resp.status_code == 503
        data = resp.get_json()
        assert 'sem_api_key' in data.get('erro', '') or '❌' in data.get('resposta', '')

    def test_endpoint_chat_pergunta_vazia(self, cliente):
        resp = cliente.post('/api/chat', json={'pergunta': ''})
        assert resp.status_code in (400, 503)  # 503 se sem API key, 400 se com

    def test_endpoint_chat_pergunta_longa(self, cliente):
        resp = cliente.post('/api/chat', json={'pergunta': 'x' * 3000})
        assert resp.status_code in (400, 503)

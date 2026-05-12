/**
 * app.js — Lógica do Dashboard Prospector Bets
 * Responsável por: fetch de dados, filtros, tabela paginada,
 * ordenação, gráficos Chart.js, exportação CSV/XLSX.
 */

'use strict';

// ---------------------------------------------------------------------------
// Estado global
// ---------------------------------------------------------------------------

let todosOsDados = [];         // todos os registros carregados da API
let dadosFiltrados = [];       // subconjunto após filtros
let paginaAtual = 1;
let tamanhoPagina = 25;
let colunaOrdem = '';
let ordemAsc = true;

// Agrupamento de status brutos em 3 categorias visíveis
const STATUS_GROUPS = {
  com_email: ['encontrado', 'encontrado_js', 'encontrado_manual'],
  sem_email: ['nao_encontrado'],
  falhou:    ['erro_conexao', 'bloqueado_robots', 'sem_url'],
};

// Paleta de cores para gráficos
const CORES = [
  '#3b82f6', '#22c55e', '#f59e0b', '#ef4444', '#a855f7',
  '#06b6d4', '#f97316', '#ec4899', '#84cc16', '#64748b',
];

// ---------------------------------------------------------------------------
// Inicialização
// ---------------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
  carregarDados();
  iniciarPollingUrlHealth();
  iniciarPollingAfiliadosHealth();
  iniciarPollingReclameAqui();
  carregarSparklines();
});

async function carregarDados() {
  mostrarLoading(true);
  try {
    const [dadosResp, statsResp] = await Promise.all([
      fetch('/api/dados'),
      fetch('/api/stats'),
    ]);
    todosOsDados = await dadosResp.json();
    const stats = await statsResp.json();

    preencherKPIs(stats);
    preencherFiltrosDropdown(stats);
    dadosFiltrados = [...todosOsDados];
    renderizarTabela();
    renderizarGraficos();
  } catch (err) {
    console.error('Erro ao carregar dados:', err);
    document.getElementById('table-count').textContent = 'Erro ao carregar dados.';
  } finally {
    mostrarLoading(false);
  }
}

async function recarregarDados() {
  await fetch('/api/recarregar', { method: 'POST' });
  await carregarDados();
}

/**
 * Re-sincroniza dados/stats do servidor após uma edição.
 * Preserva o filtro corrente (reaplica aplicarFiltros se algum filtro
 * estiver ativo) e mantém a ordenação/página atual.
 */
async function ressincronizar() {
  const [dadosResp, statsResp] = await Promise.all([
    fetch('/api/dados'),
    fetch('/api/stats'),
  ]);
  todosOsDados = await dadosResp.json();
  const stats = await statsResp.json();
  preencherKPIs(stats);

  if (filtroAtivo()) {
    // Reaplica filtros sobre os dados novos — preserva o que o usuário filtrou
    aplicarFiltros();
  } else {
    dadosFiltrados = [...todosOsDados];
    // Reaplica ordenação se havia
    if (colunaOrdem) {
      const asc = ordemAsc;
      ordemAsc = !asc; // ordenarPor alterna, então forçamos voltar ao mesmo estado
      ordenarPor(colunaOrdem);
    }
    renderizarTabela();
    renderizarGraficos();
  }
}

function filtroAtivo() {
  const campos = ['f-marca', 'f-status', 'f-afiliados', 'f-porte', 'f-situacao',
                  'f-uf', 'f-municipio', 'f-data-inicio', 'f-data-fim', 'f-saude-url',
                  'f-reclame-aqui'];
  return campos.some(id => (document.getElementById(id)?.value || '').trim() !== '');
}

function cssEscape(s) {
  if (window.CSS && CSS.escape) return CSS.escape(s);
  return String(s).replace(/[^\w-]/g, '\\$&');
}

// ---------------------------------------------------------------------------
// KPI Cards
// ---------------------------------------------------------------------------

function preencherKPIs(stats) {
  document.getElementById('kpi-total').textContent     = stats.total ?? '—';
  document.getElementById('kpi-email').textContent     = stats.com_email ?? '—';
  document.getElementById('kpi-sem-email').textContent = stats.sem_email ?? '—';
  document.getElementById('kpi-afiliados').textContent = stats.com_afiliados ?? '—';
  document.getElementById('kpi-editados').textContent  = stats.editados_manualmente ?? '—';
  document.getElementById('kpi-atualizacao').textContent =
    stats.ultima_atualizacao ? stats.ultima_atualizacao.substring(0, 16) : '—';

  // KPIs de saúde das URLs
  // urls_ativas = online total (ok + redirect + bloqueado)
  const ativas = document.getElementById('kpi-urls-ativas');
  if (ativas) {
    ativas.textContent = stats.urls_ativas ?? '—';
    // Tooltip mostra breakdown dos 3 sub-status
    const kpiCard = ativas.closest('.kpi-card') || ativas.parentElement;
    if (kpiCard) {
      const ok  = stats.urls_ok        ?? 0;
      const rd  = stats.urls_redirect  ?? 0;
      const blk = stats.urls_bloqueadas ?? 0;
      kpiCard.title =
        `Sites online (acessíveis por usuários reais)\n` +
        `🟢 ${ok} respondem 200 OK\n` +
        `🟡 ${rd} via redirect (30x)\n` +
        `🟠 ${blk} bloqueiam bots (4xx)`;
    }
  }
  const inativas = document.getElementById('kpi-urls-inativas');
  if (inativas) inativas.textContent = stats.urls_inativas ?? '—';

  // Indicador Playwright
  const pwEl = document.getElementById('playwright-status');
  if (pwEl) {
    if (stats.playwright_disponivel) {
      pwEl.innerHTML = '<span style="color:#22c55e;font-size:.75rem" title="Playwright instalado — coleta JS ativa">&#9679; PW</span>';
    } else {
      pwEl.innerHTML = '<span style="color:#ef4444;font-size:.75rem" title="Playwright não instalado — coleta JS inativa">&#9679; PW</span>';
    }
  }

  // Status do sync com CSV oficial
  const sindEl = document.getElementById('sync-status');
  if (sindEl && stats.csv_sync) {
    const s = stats.csv_sync;
    if (s.ultimo_sync) {
      const quando = formatarDesdeQuando(s.ultimo_sync);
      const detalhe = (s.adicionadas || s.removidas || s.url_atualizada)
        ? ` · +${s.adicionadas} -${s.removidas} ~${s.url_atualizada}`
        : ' · sem mudanças';
      sindEl.textContent = `${quando}${detalhe}`;
      sindEl.parentElement.classList.toggle('sync-ok', !!s.sucesso);
      sindEl.parentElement.classList.toggle('sync-erro', !s.sucesso);
    } else {
      sindEl.textContent = 'nunca';
    }
  }
}

async function forcarSyncCsv() {
  const btn = event?.target;
  if (btn) { btn.disabled = true; btn.textContent = '⟳ Sincronizando…'; }
  try {
    const resp = await fetch('/api/csv-sync-agora', { method: 'POST' });
    const r = await resp.json();
    if (r.sucesso) {
      const msg = `CSV sincronizado.\n` +
        `+ ${r.adicionadas.length} adicionadas\n` +
        `- ${r.removidas.length} removidas\n` +
        `~ ${r.url_atualizada.length} URLs atualizadas`;
      alert(msg);
      await carregarDados();
    } else {
      alert('Falha ao sincronizar: ' + (r.erro || 'erro desconhecido'));
    }
  } catch (e) {
    alert('Erro: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '⟲ Sincronizar CSV'; }
  }
}

// ---------------------------------------------------------------------------
// Preenchimento dos dropdowns de filtro
// ---------------------------------------------------------------------------

function preencherFiltrosDropdown(stats) {
  preencherSelect('f-porte',     stats.portes    || []);
  preencherSelect('f-situacao',  stats.situacoes || []);
  preencherSelect('f-uf',        stats.ufs       || []);
}

function preencherSelect(id, valores) {
  const sel = document.getElementById(id);
  const primeiraOpcao = sel.options[0]; // mantém o "Todos/Todas"
  sel.innerHTML = '';
  sel.appendChild(primeiraOpcao);
  valores.forEach(v => {
    if (!v) return;
    const opt = document.createElement('option');
    opt.value = v;
    opt.textContent = v;
    sel.appendChild(opt);
  });
}

// Cascata UF → Município
async function atualizarMunicipios() {
  const uf = document.getElementById('f-uf').value;
  const sel = document.getElementById('f-municipio');
  sel.innerHTML = '<option value="">Todos</option>';
  if (!uf) return;

  const municipios = todosOsDados
    .filter(r => (r.uf || '').toUpperCase() === uf.toUpperCase() && r.municipio)
    .map(r => r.municipio);
  const unicos = [...new Set(municipios)].sort();

  unicos.forEach(m => {
    const opt = document.createElement('option');
    opt.value = m;
    opt.textContent = m;
    sel.appendChild(opt);
  });
}

// ---------------------------------------------------------------------------
// Filtros
// ---------------------------------------------------------------------------

function aplicarFiltros() {
  const marca       = document.getElementById('f-marca').value.toLowerCase().trim();
  const status      = document.getElementById('f-status').value;
  const afiliados   = document.getElementById('f-afiliados').value;
  const porte       = document.getElementById('f-porte').value;
  const situacao    = document.getElementById('f-situacao').value;
  const uf          = document.getElementById('f-uf').value;
  const municipio   = document.getElementById('f-municipio').value;
  const dataInicio  = document.getElementById('f-data-inicio').value;
  const dataFim     = document.getElementById('f-data-fim').value;
  const saudeUrl    = document.getElementById('f-saude-url')?.value || '';
  const fRa         = document.getElementById('f-reclame-aqui')?.value || '';

  dadosFiltrados = todosOsDados.filter(r => {
    if (marca && !(r.marca || '').toLowerCase().includes(marca)) return false;
    if (status) {
      if (status === 'com_email') {
        if (!r.email_contato) return false;
      } else if (status === 'sem_email') {
        if (r.email_contato) return false;
      } else if (status === 'falhou') {
        const grupo = STATUS_GROUPS['falhou'];
        if (!grupo || !grupo.includes(r.status)) return false;
      }
    }
    if (afiliados) {
      const disp = r._afiliados_display || 'nao_encontrado';
      if (afiliados === 'com' && disp !== 'sim') return false;
      if (afiliados === 'sem' && disp !== 'nao') return false;
      if (afiliados === 'nd'  && disp !== 'nao_encontrado') return false;
    }
    if (porte && r.porte_empresa !== porte) return false;
    if (situacao && r.situacao_cadastral !== situacao) return false;
    if (uf && (r.uf || '').toUpperCase() !== uf.toUpperCase()) return false;
    if (municipio && r.municipio !== municipio) return false;
    if (dataInicio && (r.data_coleta || '').substring(0, 10) < dataInicio) return false;
    if (dataFim && (r.data_coleta || '').substring(0, 10) > dataFim) return false;
    if (saudeUrl) {
      const st    = r._url_health_status || 'desconhecido';
      const ONLINE = ['ok', 'redirect', 'bloqueado'];                      // site acessível p/ usuários reais
      const ERROS  = ['erro_http','erro_conexao','erro_ssl','erro_dns','timeout','erro'];
      if (saudeUrl === 'online'      && !ONLINE.includes(st))   return false;  // todas as acessíveis
      if (saudeUrl === 'ok'          && st !== 'ok')            return false;  // 200 direto
      if (saudeUrl === 'redirect'    && st !== 'redirect')      return false;  // 30x
      if (saudeUrl === 'bloqueado'   && st !== 'bloqueado')     return false;  // 4xx bots
      if (saudeUrl === 'erro'        && !ERROS.includes(st))    return false;  // 5xx/offline
      if (saudeUrl === 'desconhecido'&& st !== 'desconhecido')  return false;  // ainda não checada
    }
    if (fRa) {
      const raSt = r._ra_status || 'desconhecido';
      if (fRa === 'encontrado'     && raSt !== 'encontrado')     return false;
      if (fRa === 'ra1000'         && !r._ra_ra1000)             return false;
      if (fRa === 'nao_encontrado' && raSt !== 'nao_encontrado') return false;
      if (fRa === 'desconhecido'   && raSt !== 'desconhecido')   return false;
    }
    return true;
  });

  paginaAtual = 1;
  renderizarTabela();
  renderizarGraficos();
}

function limparFiltros() {
  ['f-marca', 'f-data-inicio', 'f-data-fim'].forEach(id => {
    document.getElementById(id).value = '';
  });
  ['f-status', 'f-afiliados', 'f-porte', 'f-situacao', 'f-uf', 'f-municipio', 'f-saude-url', 'f-reclame-aqui'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.selectedIndex = 0;
  });
  dadosFiltrados = [...todosOsDados];
  paginaAtual = 1;
  renderizarTabela();
  renderizarGraficos();
}

// ---------------------------------------------------------------------------
// Ordenação
// ---------------------------------------------------------------------------

function ordenarPor(coluna) {
  if (colunaOrdem === coluna) {
    ordemAsc = !ordemAsc;
  } else {
    colunaOrdem = coluna;
    ordemAsc = true;
  }

  // Atualiza ícones de cabeçalho
  document.querySelectorAll('.data-table th').forEach(th => {
    th.classList.remove('sort-asc', 'sort-desc');
  });
  const th = document.querySelector(`[data-col="${coluna}"]`);
  if (th) th.classList.add(ordemAsc ? 'sort-asc' : 'sort-desc');

  const numerica = coluna === 'capital_social';
  dadosFiltrados.sort((a, b) => {
    if (numerica) {
      const na = parseFloat(a[coluna]) || 0;
      const nb = parseFloat(b[coluna]) || 0;
      return ordemAsc ? na - nb : nb - na;
    }
    const va = (a[coluna] || '').toString().toLowerCase();
    const vb = (b[coluna] || '').toString().toLowerCase();
    if (va < vb) return ordemAsc ? -1 : 1;
    if (va > vb) return ordemAsc ?  1 : -1;
    return 0;
  });

  paginaAtual = 1;
  renderizarTabela();
}

// ---------------------------------------------------------------------------
// Tabela
// ---------------------------------------------------------------------------

function renderizarTabela() {
  const total = dadosFiltrados.length;
  const inicio = (paginaAtual - 1) * tamanhoPagina;
  const fim = Math.min(inicio + tamanhoPagina, total);
  const pagina = dadosFiltrados.slice(inicio, fim);

  const tbody = document.getElementById('table-body');
  tbody.innerHTML = '';

  if (total === 0) {
    tbody.innerHTML = `<tr><td colspan="14" class="empty-state">Nenhum resultado para os filtros aplicados.</td></tr>`;
    document.getElementById('table-count').textContent = '0 registros encontrados';
    document.getElementById('pagination').style.display = 'none';
    document.getElementById('table-wrapper').style.display = 'block';
    return;
  }

  pagina.forEach(r => {
    const temEmail = r.status === 'encontrado' || r.status === 'encontrado_js' || r.status === 'encontrado_manual';
    const tr = document.createElement('tr');
    tr.className = temEmail ? 'row-success' : (r.status === 'nao_encontrado' ? 'row-danger' : '');
    if (r._editado_manualmente) tr.classList.add('row-edited');
    tr.dataset.cnpj = r.cnpj || '';

    const editadoBadge = r._editado_manualmente
      ? ` <span class="badge-edit" title="Editado manualmente em ${esc(r._editado_em || '')}">M</span>`
      : '';
    const inativaBadge = r._url_inativa
      ? ` <span class="badge-inativa" title="URL inacessível — site offline, DNS falhou, timeout ou erro 5xx">INATIVA</span>`
      : '';
    const removidaBadge = r._removido_do_csv
      ? ` <span class="badge-removida" title="Bet removida da lista oficial do gov.br em ${esc(r._removido_em || '')}">REMOVIDA</span>`
      : '';
    if (r._url_inativa) tr.classList.add('row-inativa');

    tr.innerHTML = `
      <td title="${esc(r.marca)}">${esc(r.marca)}${editadoBadge}${inativaBadge}${removidaBadge}</td>
      <td title="${esc(r.razao_social)}">${celulaEditavel(r, 'razao_social')}</td>
      <td>${esc(r.cnpj)}</td>
      <td class="url-cell" data-url="${esc(r.url || '')}">${celulaEditavel(r, 'url', 'url')}${urlHealthDot(r)}</td>
      <td>${celulaEditavel(r, 'email_contato', 'email')}</td>
      <td>${badgeAfiliados(r)}</td>
      <td>${badgeReclameAqui(r)}</td>
      <td>${esc(r.uf) || '—'}</td>
      <td title="${esc(r.municipio)}">${esc(r.municipio) || '—'}</td>
      <td>${badgeStatus(r.status)}</td>
      <td class="num-col" title="${formatarBRL(r.capital_social, true)}">${formatarBRL(r.capital_social)}</td>
      <td>${formatarData(r.data_abertura)}</td>
      <td>${formatarData(r.data_coleta)}</td>
      <td class="obs-col">${celulaObservacao(r)}</td>
    `;
    tbody.appendChild(tr);
  });

  // Ativa edição inline em todas as células editáveis
  tbody.querySelectorAll('.cell-editable').forEach(el => {
    el.addEventListener('click', ev => {
      if (ev.target.closest('a.table-link')) return;
      iniciarEdicaoInline(el);
    });
  });

  // Ativa edição de observações
  tbody.querySelectorAll('.obs-preview').forEach(el => {
    el.addEventListener('click', ev => {
      ev.stopPropagation();
      iniciarEdicaoObservacao(el);
    });
  });

  document.getElementById('table-count').textContent =
    `${total.toLocaleString('pt-BR')} registros (exibindo ${inicio + 1}–${fim})`;

  document.getElementById('table-wrapper').style.display = 'block';
  renderizarPaginacao(total);
}

// ---------------------------------------------------------------------------
// Edição inline de células (email, url, etc.)
// ---------------------------------------------------------------------------

function celulaEditavel(r, campo, tipo = 'text') {
  const valor = r[campo];
  const cnpj = r.cnpj || '';
  const editado = (r._campos_editados || []).includes(campo);
  const marcaEdit = editado ? '<span class="edit-dot" title="Editado manualmente"></span>' : '';

  if (!valor) {
    return `<span class="cell-editable cell-empty" data-campo="${campo}" data-tipo="${tipo}" data-cnpj="${esc(cnpj)}" title="Clique para adicionar">+ adicionar</span>`;
  }

  let conteudo;
  if (tipo === 'email') {
    conteudo = `<a class="table-link" href="mailto:${esc(valor)}" onclick="event.stopPropagation()">${esc(valor)}</a>`;
  } else if (tipo === 'url') {
    conteudo = `<a class="table-link" href="${esc(valor)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${urlCurta(valor)}</a>`;
  } else {
    conteudo = esc(valor);
  }

  return `<span class="cell-editable" data-campo="${campo}" data-tipo="${tipo}" data-cnpj="${esc(cnpj)}" data-valor="${esc(valor)}" title="Clique para editar">${conteudo}${marcaEdit}</span>`;
}

function celulaObservacao(r) {
  const valor = r.observacao || '';
  const cnpj  = r.cnpj || '';
  const preview = valor
    ? `<span class="obs-preview" data-cnpj="${esc(cnpj)}" data-valor="${esc(valor)}" title="Clique para editar">${esc(valor)}</span>`
    : `<span class="obs-preview obs-empty" data-cnpj="${esc(cnpj)}" data-valor="" title="Clique para adicionar observação">+ nota</span>`;
  return preview;
}

function iniciarEdicaoObservacao(el) {
  if (el.classList.contains('editing-obs')) return;
  const cnpj  = el.dataset.cnpj;
  const valorAtual = el.dataset.valor || '';
  const htmlOriginal = el.outerHTML;

  el.classList.add('editing-obs');
  el.style.display = 'none';

  const wrapper = document.createElement('div');
  wrapper.style.cssText = 'display:flex;flex-direction:column;gap:4px;min-width:200px';

  const textarea = document.createElement('textarea');
  textarea.className = 'obs-textarea';
  textarea.value = valorAtual;
  textarea.placeholder = 'Observação sobre esta empresa...';
  textarea.rows = 3;

  const btns = document.createElement('div');
  btns.style.cssText = 'display:flex;gap:6px';

  const btnSave   = document.createElement('button');
  btnSave.textContent = '✓ Salvar';
  btnSave.className = 'btn-inline-ok';

  const btnCancel = document.createElement('button');
  btnCancel.textContent = '✕ Cancelar';
  btnCancel.className = 'btn-inline-cancel';

  btns.appendChild(btnSave);
  btns.appendChild(btnCancel);
  wrapper.appendChild(textarea);
  wrapper.appendChild(btns);
  el.parentNode.insertBefore(wrapper, el.nextSibling);
  setTimeout(() => { textarea.focus(); textarea.setSelectionRange(textarea.value.length, textarea.value.length); }, 0);

  const cancelar = () => {
    wrapper.remove();
    el.style.display = '';
    el.classList.remove('editing-obs');
  };

  const salvar = async () => {
    const novoValor = textarea.value.trim();
    if (novoValor === valorAtual) { cancelar(); return; }
    btnSave.disabled = true;
    btnSave.textContent = '…';
    try {
      const resp = await fetch('/api/editar', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cnpj, campo: 'observacao', valor: novoValor }),
      });
      const data = await resp.json();
      if (!data.ok) throw new Error(data.erro || 'Erro desconhecido');
      await ressincronizar();
    } catch (e) {
      alert('Erro ao salvar observação: ' + e.message);
      btnSave.disabled = false;
      btnSave.textContent = '✓ Salvar';
    }
  };

  btnSave.addEventListener('click', (e) => { e.stopPropagation(); salvar(); });
  btnCancel.addEventListener('click', (e) => { e.stopPropagation(); cancelar(); });
  textarea.addEventListener('keydown', (e) => {
    e.stopPropagation();
    if (e.key === 'Escape') cancelar();
    if (e.key === 'Enter' && e.ctrlKey) salvar();
  });
}

function iniciarEdicaoInline(el) {
  if (el.classList.contains('editing')) return;
  const campo = el.dataset.campo;
  const cnpj  = el.dataset.cnpj;
  const tipo  = el.dataset.tipo || 'text';
  const valorAtual = el.dataset.valor || '';

  if (!cnpj) {
    alert('Registro sem CNPJ — não é possível editar.');
    return;
  }

  // Preserva estado original para cancelamento
  const htmlOriginal = el.innerHTML;
  const eraEmpty = el.classList.contains('cell-empty');

  // Remove cell-empty durante edição (CSS dessa classe cascateia no input)
  el.classList.remove('cell-empty');
  el.classList.add('editing');
  el.removeAttribute('title');
  el.innerHTML = '';

  // Cria nós via DOM API (mais robusto que innerHTML com template literal)
  const input = document.createElement('input');
  input.type = tipo === 'email' ? 'email' : (tipo === 'url' ? 'url' : 'text');
  input.className = 'inline-edit-input';
  input.value = valorAtual;
  input.autocomplete = 'off';
  input.spellcheck = false;

  const btnSave = document.createElement('button');
  btnSave.type = 'button';
  btnSave.className = 'inline-edit-save';
  btnSave.title = 'Salvar';
  btnSave.textContent = '✓';

  const btnCancel = document.createElement('button');
  btnCancel.type = 'button';
  btnCancel.className = 'inline-edit-cancel';
  btnCancel.title = 'Cancelar';
  btnCancel.textContent = '✕';

  el.appendChild(input);
  el.appendChild(btnSave);
  el.appendChild(btnCancel);

  // Foco assíncrono para evitar que o click original roube o foco
  setTimeout(() => { input.focus(); input.select(); }, 0);

  let finalizado = false;

  const restaurarOriginal = () => {
    el.classList.remove('editing');
    if (eraEmpty) el.classList.add('cell-empty');
    el.innerHTML = htmlOriginal;
    if (eraEmpty) {
      el.setAttribute('title', 'Clique para adicionar');
    } else if (valorAtual) {
      el.setAttribute('title', 'Clique para editar');
    }
  };

  const cancelar = () => {
    if (finalizado) return;
    finalizado = true;
    restaurarOriginal();
  };

  const salvar = async () => {
    if (finalizado) return;
    const novoValor = input.value.trim();
    if (novoValor === valorAtual) { cancelar(); return; }

    // Se o usuário esvaziou o campo, pede confirmação explícita de exclusão
    if (novoValor === '' && valorAtual !== '') {
      const ok = confirm(`Remover o valor atual "${valorAtual}" deste registro?`);
      if (!ok) { input.focus(); return; }
    }

    finalizado = true;
    btnSave.disabled = true;
    btnCancel.disabled = true;
    btnSave.textContent = '…';
    try {
      const resp = await fetch('/api/editar', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cnpj, campo, valor: novoValor }),
      });
      const data = await resp.json();
      if (!data.ok) throw new Error(data.erro || 'Erro desconhecido');

      // Re-sincroniza TUDO do servidor — garante que overrides aplicados
      // no backend (inclui mudança de status para 'encontrado_manual')
      // fiquem refletidos em memória e sobrevivam a filtros/ordenações.
      await ressincronizar();

      // Pisca a linha editada para feedback visual
      const linhaEditada = document.querySelector(`tr[data-cnpj="${cssEscape(cnpj)}"]`);
      if (linhaEditada) {
        linhaEditada.classList.add('row-flash');
        setTimeout(() => linhaEditada.classList.remove('row-flash'), 1500);
      }
    } catch (e) {
      alert('Erro ao salvar: ' + e.message);
      finalizado = false;
      btnSave.disabled = false;
      btnCancel.disabled = false;
      btnSave.textContent = '✓';
    }
  };

  // stopPropagation em mousedown evita que o click listener do span reentre
  const pararPropagacao = (ev) => ev.stopPropagation();
  input.addEventListener('mousedown', pararPropagacao);
  input.addEventListener('click', pararPropagacao);
  btnSave.addEventListener('mousedown', pararPropagacao);
  btnCancel.addEventListener('mousedown', pararPropagacao);

  btnSave.addEventListener('click', (ev) => {
    ev.stopPropagation();
    ev.preventDefault();
    salvar();
  });
  btnCancel.addEventListener('click', (ev) => {
    ev.stopPropagation();
    ev.preventDefault();
    cancelar();
  });
  input.addEventListener('keydown', (ev) => {
    ev.stopPropagation();
    if (ev.key === 'Enter') { ev.preventDefault(); salvar(); }
    else if (ev.key === 'Escape') { ev.preventDefault(); cancelar(); }
  });
}

// ---------------------------------------------------------------------------
// Paginação
// ---------------------------------------------------------------------------

function renderizarPaginacao(total) {
  const totalPaginas = Math.ceil(total / tamanhoPagina);
  const paginacaoEl = document.getElementById('pagination');
  const numerosEl   = document.getElementById('page-numbers');

  if (totalPaginas <= 1) {
    paginacaoEl.style.display = 'none';
    return;
  }

  paginacaoEl.style.display = 'flex';
  numerosEl.innerHTML = '';

  const btnPrev = document.getElementById('btn-prev');
  const btnNext = document.getElementById('btn-next');
  btnPrev.disabled = paginaAtual === 1;
  btnNext.disabled = paginaAtual === totalPaginas;

  // Mostra no máximo 7 botões de página com reticências
  const paginas = paginasVisiveis(paginaAtual, totalPaginas);
  paginas.forEach(p => {
    if (p === '...') {
      const span = document.createElement('span');
      span.textContent = '…';
      span.style.cssText = 'padding:0 6px;color:var(--text-muted);line-height:32px';
      numerosEl.appendChild(span);
      return;
    }
    const btn = document.createElement('button');
    btn.className = 'page-btn' + (p === paginaAtual ? ' active' : '');
    btn.textContent = p;
    btn.onclick = () => irParaPagina(p);
    numerosEl.appendChild(btn);
  });
}

function paginasVisiveis(atual, total) {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  const paginas = [];
  if (atual <= 4) {
    for (let i = 1; i <= 5; i++) paginas.push(i);
    paginas.push('...', total);
  } else if (atual >= total - 3) {
    paginas.push(1, '...');
    for (let i = total - 4; i <= total; i++) paginas.push(i);
  } else {
    paginas.push(1, '...', atual - 1, atual, atual + 1, '...', total);
  }
  return paginas;
}

function irParaPagina(p) {
  const total = Math.ceil(dadosFiltrados.length / tamanhoPagina);
  if (p < 1 || p > total) return;
  paginaAtual = p;
  renderizarTabela();
  document.querySelector('.content, .main-content')?.scrollIntoView({ behavior: 'smooth' });
}

function mudarTamanhoPagina() {
  tamanhoPagina = parseInt(document.getElementById('page-size').value);
  paginaAtual = 1;
  renderizarTabela();
}

// ---------------------------------------------------------------------------
// Gráficos
// ---------------------------------------------------------------------------

function renderizarGraficos() {
  renderizarGraficoPizza('chart-porte', 'porte_empresa');
  renderizarGraficoStatus();
  renderizarGraficoBarras();
}

/**
 * Gráfico "Status de Email" — agrupa os status brutos em 3 categorias legíveis
 * em vez de usar os valores raw do campo (que mostram "encontrado_manual", "Não identificado" etc).
 */
function renderizarGraficoStatus() {
  const grupos = { 'Com email': 0, 'Sem email': 0, 'Falhou': 0 };
  const STATUS_FALHOU = ['erro_conexao', 'bloqueado_robots', 'sem_url'];

  dadosFiltrados.forEach(r => {
    if (r.email_contato) {
      grupos['Com email']++;
    } else if (STATUS_FALHOU.includes(r.status)) {
      grupos['Falhou']++;
    } else {
      grupos['Sem email']++;
    }
  });

  // Remove categorias com 0 (não polui a legenda)
  const labels  = Object.keys(grupos).filter(k => grupos[k] > 0);
  const valores = labels.map(k => grupos[k]);

  const ctx = document.getElementById('chart-status');
  if (!ctx) return;
  const instancia = Chart.getChart('chart-status');
  if (instancia) instancia.destroy();

  new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels,
      datasets: [{
        data: valores,
        backgroundColor: ['#22c55e', '#ef4444', '#f59e0b'].slice(0, labels.length),
        borderColor: '#1e293b',
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: 'bottom',
          labels: { color: '#94a3b8', font: { size: 11 }, padding: 10, boxWidth: 12 },
        },
        tooltip: estiloTooltip(),
      },
    },
  });
}

function renderizarGraficoPizza(canvasId, campo, donut = false) {
  const contagem = contarPor(campo);
  const labels = Object.keys(contagem);
  const valores = Object.values(contagem);

  const ctx = document.getElementById(canvasId);
  if (!ctx) return;

  // Destrói instância anterior para evitar sobreposição
  const instancia = Chart.getChart(canvasId);
  if (instancia) instancia.destroy();

  new Chart(ctx, {
    type: donut ? 'doughnut' : 'pie',
    data: {
      labels,
      datasets: [{
        data: valores,
        backgroundColor: CORES.slice(0, labels.length),
        borderColor: '#1e293b',
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: 'bottom',
          labels: {
            color: '#94a3b8',
            font: { size: 11 },
            padding: 10,
            boxWidth: 12,
          },
        },
        tooltip: estiloTooltip(),
      },
    },
  });
}

function renderizarGraficoBarras() {
  const contagem = contarPor('uf');
  const sorted = Object.entries(contagem).sort((a, b) => b[1] - a[1]);
  const labels  = sorted.map(([k]) => k || 'N/A');
  const valores = sorted.map(([, v]) => v);

  const ctx = document.getElementById('chart-uf');
  if (!ctx) return;

  const instancia = Chart.getChart('chart-uf');
  if (instancia) instancia.destroy();

  new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        label: 'Bets',
        data: valores,
        backgroundColor: CORES[0] + 'cc',
        borderColor: CORES[0],
        borderWidth: 1,
        borderRadius: 4,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: estiloTooltip(),
      },
      scales: {
        x: {
          ticks: { color: '#94a3b8', font: { size: 11 } },
          grid: { color: 'rgba(51,65,85,.4)' },
        },
        y: {
          ticks: { color: '#94a3b8', font: { size: 11 }, stepSize: 1 },
          grid: { color: 'rgba(51,65,85,.4)' },
          beginAtZero: true,
        },
      },
    },
  });
}

function estiloTooltip() {
  return {
    backgroundColor: '#1e293b',
    borderColor: '#334155',
    borderWidth: 1,
    titleColor: '#e2e8f0',
    bodyColor: '#94a3b8',
    padding: 10,
  };
}

function contarPor(campo) {
  return dadosFiltrados.reduce((acc, r) => {
    const val = r[campo] || 'Não identificado';
    acc[val] = (acc[val] || 0) + 1;
    return acc;
  }, {});
}

// ---------------------------------------------------------------------------
// Exportação CSV
// ---------------------------------------------------------------------------

function exportarCSV() {
  if (!dadosFiltrados.length) return alert('Nenhum dado para exportar.');

  const colunas = [
    'marca', 'razao_social', 'cnpj', 'url', 'email_contato', 'status',
    'url_afiliados', 'status_afiliados',
    'regime_tributario', 'porte_empresa', 'situacao_cadastral', 'capital_social',
    'natureza_juridica', 'data_abertura', 'logradouro', 'numero', 'complemento',
    'bairro', 'municipio', 'uf', 'cep', 'pais',
    'fonte_regime', 'confiabilidade_dado', 'data_coleta', 'observacao',
  ];

  const header = colunas.join(',');
  const linhas = dadosFiltrados.map(r =>
    colunas.map(c => csvCelula(r[c])).join(',')
  );

  const conteudo = [header, ...linhas].join('\r\n');
  const blob = new Blob(['\uFEFF' + conteudo], { type: 'text/csv;charset=utf-8' });
  download(blob, `bets_${timestamp()}.csv`);
}

// ---------------------------------------------------------------------------
// Exportação XLSX
// ---------------------------------------------------------------------------

function exportarXLSX() {
  if (!dadosFiltrados.length) return alert('Nenhum dado para exportar.');
  if (typeof XLSX === 'undefined') return alert('Biblioteca XLSX não carregada.');

  // Remove campos internos (_*) antes de exportar
  const dadosLimpos = dadosFiltrados.map(r => {
    const obj = {};
    Object.keys(r).forEach(k => { if (!k.startsWith('_')) obj[k] = r[k]; });
    return obj;
  });

  const ws = XLSX.utils.json_to_sheet(dadosLimpos);
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, 'Bets');

  // Ajusta largura das colunas automaticamente
  if (ws['!ref']) {
    const range = XLSX.utils.decode_range(ws['!ref']);
    ws['!cols'] = Array.from({ length: range.e.c + 1 }, (_, i) => {
      let max = 10;
      for (let r = range.s.r; r <= range.e.r; r++) {
        const cell = ws[XLSX.utils.encode_cell({ r, c: i })];
        if (cell && cell.v) max = Math.max(max, String(cell.v).length);
      }
      return { wch: Math.min(max + 2, 40) };
    });
  }

  XLSX.writeFile(wb, `bets_${timestamp()}.xlsx`);
}

// ---------------------------------------------------------------------------
// Utilitários
// ---------------------------------------------------------------------------

function mostrarLoading(ativo) {
  document.getElementById('loading').style.display       = ativo ? 'flex' : 'none';
  document.getElementById('table-wrapper').style.display = ativo ? 'none' : 'block';
  if (ativo) document.getElementById('pagination').style.display = 'none';
}

function esc(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function urlCurta(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return url;
  }
}

// ---------------------------------------------------------------------------
// Indicador de saúde da URL (🟢 ok, 🟡 redirect, 🔴 erro, ⚪ desconhecido)
// ---------------------------------------------------------------------------

const URL_HEALTH_CLASS = {
  ok:           'url-dot-ok',
  redirect:     'url-dot-redirect',
  bloqueado:    'url-dot-bloqueado',   // site ativo mas bloqueando bots (403/4xx)
  erro_http:    'url-dot-erro',
  erro_conexao: 'url-dot-erro',
  erro_ssl:     'url-dot-erro',
  erro_dns:     'url-dot-erro',
  timeout:      'url-dot-erro',
  erro:         'url-dot-erro',
  desconhecido: 'url-dot-desconhecido',
};

const URL_HEALTH_LABEL = {
  ok:           'URL ativa',
  redirect:     'Redireciona para outro domínio',
  bloqueado:    'Site ativo (bloqueia verificação automática)',
  erro_http:    'Erro HTTP no servidor (5xx)',
  erro_conexao: 'Erro de conexão',
  erro_ssl:     'Erro de certificado SSL',
  erro_dns:     'Domínio não resolvido (DNS)',
  timeout:      'Timeout',
  erro:         'Erro ao validar',
  desconhecido: 'Ainda não checado',
};

function formatarDesdeQuando(iso) {
  if (!iso) return '';
  try {
    const t = new Date(iso).getTime();
    const seg = Math.max(0, Math.floor((Date.now() - t) / 1000));
    if (seg < 60)   return `há ${seg}s`;
    if (seg < 3600) return `há ${Math.floor(seg / 60)} min`;
    return `há ${Math.floor(seg / 3600)} h`;
  } catch { return ''; }
}

function urlHealthDot(r) {
  const st = r._url_health_status || 'desconhecido';
  const cls = URL_HEALTH_CLASS[st] || 'url-dot-desconhecido';
  const label = URL_HEALTH_LABEL[st] || st;
  const code = r._url_http_code ? ` · HTTP ${r._url_http_code}` : '';
  const quando = r._url_checked_at ? ` · ${formatarDesdeQuando(r._url_checked_at)}` : '';
  const redir = r._url_redirect_to ? ` → ${urlCurta(r._url_redirect_to)}` : '';
  const tooltip = esc(`${label}${code}${quando}${redir}`);
  return `<span class="url-dot ${cls}" title="${tooltip}" data-status="${st}"></span>`;
}

/**
 * Polling de saúde das URLs — a cada 15s busca /api/url-health
 * e atualiza apenas as bolinhas no DOM (não re-renderiza tabela).
 */
function iniciarPollingUrlHealth() {
  const atualizar = async () => {
    try {
      const resp = await fetch('/api/url-health');
      const health = await resp.json();
      // Atualiza dados em memória + DOM
      const STATUS_INATIVO = ['erro_http','erro_conexao','erro_ssl','erro_dns','timeout','erro'];
      let mudouInativa = false;
      todosOsDados.forEach(r => {
        const u = (r.url || '').trim();
        const info = health[u];
        if (!info) { r._url_health_status = 'desconhecido'; return; }
        const stAnterior  = r._url_health_status;
        const stRaw       = info.status || 'desconhecido';
        const httpCode    = info.http_code || 0;
        // Normaliza legado: "erro_http" com código 4xx → "bloqueado" (site ativo)
        const st = (stRaw === 'erro_http' && httpCode > 0 && httpCode < 500) ? 'bloqueado' : stRaw;
        r._url_health_status = st;
        r._url_http_code     = httpCode;
        r._url_checked_at    = info.checado_em || '';
        r._url_latencia_ms   = info.latencia_ms || 0;
        r._url_redirect_to   = info.redirecionou ? info.url_final : '';
        // Recalcula _url_inativa (st já está normalizado — "bloqueado" = ativo)
        const novoInativa = STATUS_INATIVO.includes(st) || !!r._removido_do_csv;
        if (novoInativa !== r._url_inativa) {
          r._url_inativa = novoInativa;
          mudouInativa = true;
        }
      });
      // Se _url_inativa mudou em algum registro, precisa re-renderizar tabela
      if (mudouInativa) { renderizarTabela(); return; }
      // Atualiza bolinhas visíveis
      document.querySelectorAll('tr[data-cnpj]').forEach(tr => {
        const cnpj = tr.dataset.cnpj;
        const reg = todosOsDados.find(x => (x.cnpj || '') === cnpj);
        if (!reg) return;
        const cell = tr.querySelector('td.url-cell');
        if (!cell) return;
        const dot = cell.querySelector('.url-dot');
        if (!dot) return;
        const st = reg._url_health_status || 'desconhecido';
        const cls = URL_HEALTH_CLASS[st] || 'url-dot-desconhecido';
        dot.className = `url-dot ${cls}`;
        dot.dataset.status = st;
        const label = URL_HEALTH_LABEL[st] || st;
        const code = reg._url_http_code ? ` · HTTP ${reg._url_http_code}` : '';
        const quando = reg._url_checked_at ? ` · ${formatarDesdeQuando(reg._url_checked_at)}` : '';
        const redir = reg._url_redirect_to ? ` → ${urlCurta(reg._url_redirect_to)}` : '';
        dot.title = `${label}${code}${quando}${redir}`;
      });
      // Se filtro de saúde está ativo, reaplica
      const fsaude = document.getElementById('f-saude-url');
      if (fsaude && fsaude.value) aplicarFiltros();
    } catch (err) {
      // Silencioso — se /api/url-health falha, tenta de novo no próximo tick
    }
  };
  // Primeiro tick em 3s (dá tempo de o worker rodar 1-2 vezes), depois a cada 15s
  setTimeout(atualizar, 3000);
  setInterval(atualizar, 15000);
}

// ---------------------------------------------------------------------------
// Badge de afiliados — 3 estados: Sim / Não / N/E (não encontrado)
// ---------------------------------------------------------------------------

function _displayAfiliados(status) {
  const sim = ['encontrado_completo', 'encontrado_url', 'encontrado_email'];
  const nao = ['nao_encontrado', 'bloqueado_robots'];
  if (sim.includes(status)) return 'sim';
  if (nao.includes(status)) return 'nao';
  return 'nao_encontrado';
}

function badgeAfiliados(r) {
  const d   = r._afiliados_display || 'nao_encontrado';
  const url = r._afiliados_url || r.url_afiliados || '';
  const ts  = r._afiliados_ts ? ` · verificado ${formatarDesdeQuando(r._afiliados_ts)}` : '';

  if (d === 'sim') {
    const href = url ? `href="${esc(url)}" target="_blank" rel="noopener"` : '';
    const tag  = url ? 'a' : 'span';
    return `<span class="afiliados-cell"><${tag} ${href} class="badge badge-success" title="Afiliados detectados${ts}"><span class="afiliados-dot-sim">●</span> Sim</${tag}></span>`;
  }
  if (d === 'nao') {
    return `<span class="afiliados-cell"><span class="badge badge-danger" title="Sem programa de afiliados${ts}"><span class="afiliados-dot-nao">●</span> Não</span></span>`;
  }
  return `<span class="afiliados-cell"><span class="badge badge-neutral" title="Ainda não verificado${ts}"><span class="afiliados-dot-nd">●</span> N/E</span></span>`;
}

/**
 * Polling de afiliados — a cada 30s busca /api/afiliados-health
 * e atualiza apenas as células de afiliados no DOM.
 */
function iniciarPollingAfiliadosHealth() {
  const atualizar = async () => {
    try {
      const resp = await fetch('/api/afiliados-health');
      if (!resp.ok) return;
      const dados = await resp.json();
      let mudou = false;
      todosOsDados.forEach(r => {
        const u = (r.url || '').trim();
        const info = dados[u];
        if (!info) return;
        // Não sobrescreve edições manuais
        if (r._afiliados_status === 'encontrado_manual') return;
        const novoDisplay = _displayAfiliados(info.status || '');
        if (r._afiliados_display !== novoDisplay) {
          r._afiliados_display = novoDisplay;
          r._afiliados_status  = info.status || '';
          r._afiliados_url     = info.url_afiliado || '';
          r._afiliados_ts      = info.checado_em || '';
          mudou = true;
        }
      });
      if (mudou) {
        // Reaplica filtro de afiliados se estiver ativo
        const fAfil = document.getElementById('f-afiliados');
        if (fAfil && fAfil.value) {
          aplicarFiltros();
        } else {
          renderizarTabela();
        }
      }
    } catch (_e) {
      // Silencioso — próximo tick tenta novamente
    }
  };
  // Primeiro tick em 5s, depois a cada 30s
  setTimeout(atualizar, 5000);
  setInterval(atualizar, 30000);
}

// ---------------------------------------------------------------------------
// Reclame Aqui — badge, polling e slug helper
// ---------------------------------------------------------------------------

function _marcaParaSlug(marca) {
  return (marca || '')
    .toLowerCase()
    .normalize('NFD').replace(/[̀-ͯ]/g, '')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '');
}

function badgeReclameAqui(r) {
  const st     = r._ra_status || 'desconhecido';
  const nota   = r._ra_nota;
  const rep    = r._ra_reputacao || '';
  const url    = r._ra_url || '';
  const ra1000 = r._ra_ra1000;
  const total  = r._ra_reclamacoes;
  const res    = r._ra_resolvidas;
  const ts     = r._ra_ts ? ` · verificado ${formatarDesdeQuando(r._ra_ts)}` : '';

  if (st === 'desconhecido') {
    return `<span class="ra-cell"><span class="ra-badge ra-badge-nd" title="Aguardando verificação${ts}">N/E</span></span>`;
  }
  if (st === 'nao_encontrado') {
    return `<span class="ra-cell"><span class="ra-badge ra-badge-nd" title="Não encontrado no Reclame Aqui${ts}">—</span></span>`;
  }

  // Classe de cor pela reputação
  const classMap = {
    'RA1000':          'ra-badge-ra1000',
    'Ótimo':           'ra-badge-otimo',
    'Bom':             'ra-badge-bom',
    'Regular':         'ra-badge-regular',
    'Ruim':            'ra-badge-ruim',
    'Péssimo':         'ra-badge-pessimo',
    'Sem índice':      'ra-badge-semindice',
    'Não recomendada': 'ra-badge-naorecomend',
  };
  const cls   = ra1000 ? 'ra-badge-ra1000' : (classMap[rep] || 'ra-badge-nd');
  const icon  = ra1000 ? '★ ' : '';
  const label = nota != null ? `${icon}${Number(nota).toFixed(1)}` : (rep || '?');

  const tipParts = [
    rep || '',
    nota != null ? `Nota: ${nota}/10` : '',
    total ? `${Number(total).toLocaleString('pt-BR')} reclamações` : '',
    res  != null ? `${res}% resolvidas` : '',
    ts,
  ].filter(Boolean).join(' · ');

  const inner = `<span class="ra-badge ${cls}" title="${esc(tipParts)}">${esc(label)}</span>`;
  return `<span class="ra-cell">${url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${inner}</a>` : inner}</span>`;
}

/**
 * Polling do Reclame Aqui — 1º tick em 5s, depois a cada 60s.
 * Dados mudam devagar (atualização diária no backend).
 */
function iniciarPollingReclameAqui() {
  const atualizar = async () => {
    try {
      const resp = await fetch('/api/reclame-aqui-health');
      if (!resp.ok) return;
      const dados = await resp.json();
      let mudou = false;
      todosOsDados.forEach(r => {
        const slug = _marcaParaSlug(r.marca || '');
        const info = dados[slug];
        if (!info) return;
        const novoSt = info.status || 'desconhecido';
        if (r._ra_status !== novoSt) {
          Object.assign(r, {
            _ra_status:      novoSt,
            _ra_nota:        info.nota,
            _ra_reclamacoes: info.total_reclamacoes,
            _ra_resolvidas:  info.percentual_resolvidas,
            _ra_reputacao:   info.reputacao  || '',
            _ra_ra1000:      info.ra1000     || false,
            _ra_url:         info.url_reclame_aqui || '',
            _ra_ts:          info.checado_em || '',
          });
          mudou = true;
        }
      });
      if (mudou) {
        const fRa = document.getElementById('f-reclame-aqui');
        if (fRa && fRa.value) {
          aplicarFiltros();
        } else {
          renderizarTabela();
        }
      }
    } catch (_e) {
      // Silencioso
    }
  };
  setTimeout(atualizar, 5000);
  setInterval(atualizar, 60000);
}

function badgeStatus(status) {
  // Colapsa os 7 status brutos em 3 categorias visíveis.
  // Tooltip preserva o status técnico original para debug/auditoria.
  const rotulosTecnicos = {
    'encontrado':        'Email extraído do HTML estático',
    'encontrado_js':     'Email extraído via JavaScript (Playwright)',
    'encontrado_manual': 'Email preenchido manualmente no dashboard',
    'nao_encontrado':    'Site acessado mas nenhum email foi encontrado',
    'erro_conexao':      'Não foi possível acessar o site (timeout/bloqueio)',
    'bloqueado_robots':  'robots.txt do site proíbe coleta automática',
    'sem_url':           'Registro sem URL cadastrada',
  };
  const tooltip = rotulosTecnicos[status] || status || '—';

  if (['encontrado', 'encontrado_js', 'encontrado_manual'].includes(status)) {
    return `<span class="badge badge-success" title="${esc(tooltip)}">Com email</span>`;
  }
  if (status === 'nao_encontrado') {
    return `<span class="badge badge-danger" title="${esc(tooltip)}">Sem email</span>`;
  }
  if (['erro_conexao', 'bloqueado_robots', 'sem_url'].includes(status)) {
    return `<span class="badge badge-warning" title="${esc(tooltip)}">Falhou</span>`;
  }
  return `<span class="badge badge-neutral">—</span>`;
}

function formatarBRL(valor, completo = false) {
  const n = parseFloat(valor);
  if (!n || isNaN(n)) return '—';
  if (completo) {
    return n.toLocaleString('pt-BR', { style: 'currency', currency: 'BRL' });
  }
  // Versão compacta: R$ 1,2M / R$ 350K / R$ 1.234
  if (n >= 1_000_000) return 'R$ ' + (n / 1_000_000).toFixed(1).replace('.', ',') + 'M';
  if (n >= 1_000)     return 'R$ ' + (n / 1_000).toFixed(0) + 'K';
  return 'R$ ' + n.toFixed(0);
}

function formatarData(iso) {
  if (!iso) return '—';
  const s = String(iso).substring(0, 10);
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  return m ? `${m[3]}/${m[2]}/${m[1]}` : s;
}

function csvCelula(valor) {
  const s = String(valor ?? '');
  if (s.includes(',') || s.includes('"') || s.includes('\n')) {
    return '"' + s.replace(/"/g, '""') + '"';
  }
  return s;
}

function timestamp() {
  const d = new Date();
  return d.toISOString().substring(0, 19).replace(/[T:]/g, '-');
}

function download(blob, nome) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = nome;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

// ---------------------------------------------------------------------------
// Sparklines dinâmicas — usa /api/snapshots para desenhar histórico real
// ---------------------------------------------------------------------------

async function carregarSparklines() {
  try {
    const resp = await fetch('/api/snapshots');
    if (!resp.ok) return;
    const snaps = await resp.json();
    if (!snaps || snaps.length < 2) return;   // sem histórico suficiente

    // Para cada KPI card, seleciona a série relevante e redesenha a polyline
    const mapa = [
      { svgId: 'kpi-total',        campo: 'total' },
      { svgId: 'kpi-email',        campo: 'com_email' },
      { svgId: 'kpi-sem-email',    campo: 'sem_email' },
      { svgId: 'kpi-afiliados',    campo: 'com_afiliados' },
      { svgId: 'kpi-urls-ativas',  campo: 'urls_ativas' },
      { svgId: 'kpi-urls-inativas',campo: 'urls_inativas' },
      { svgId: 'kpi-editados',     campo: 'editados' },
    ];

    mapa.forEach(({ svgId, campo }) => {
      // Encontra o KPI card que contém este elemento
      const kpiEl = document.getElementById(svgId);
      if (!kpiEl) return;
      const card = kpiEl.closest('.kpi-card');
      if (!card) return;
      const polyline = card.querySelector('svg polyline');
      if (!polyline) return;

      const valores = snaps.map(s => Number(s[campo]) || 0);
      const min = Math.min(...valores);
      const max = Math.max(...valores);
      const range = max - min || 1;

      // Mapeia para viewport SVG 100×28 (deixa 4px de margem)
      const pontos = valores.map((v, i) => {
        const x = (i / Math.max(valores.length - 1, 1)) * 100;
        const y = 4 + (1 - (v - min) / range) * 20;   // 4..24
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      });
      polyline.setAttribute('points', pontos.join(' '));
    });
  } catch (_e) {
    // Silencioso — sparklines estáticas continuam visíveis
  }
}

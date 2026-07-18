import { type FormEvent, useEffect, useState } from "react";
import {
  ApiError,
  type ApiKeyScope,
  apiKeyScopes,
  type OrganizationApiKey,
  organizationApi,
} from "../../lib/api";

type ApiKeyLoadState =
  | { status: "loading" }
  | { status: "ready"; apiKeys: OrganizationApiKey[] }
  | { status: "error"; message: string };

type RevealedKey = {
  apiKey: OrganizationApiKey;
  secret: string;
};

type PendingAction =
  | { apiKey: OrganizationApiKey; type: "rotate" }
  | { apiKey: OrganizationApiKey; type: "revoke" };

const scopeLabels: Record<ApiKeyScope, { description: string; label: string }> = {
  "agreements:write": {
    label: "Criar acordos",
    description: "Registrar acordos de custódia pela integração.",
  },
  "agreements:read": {
    label: "Ler acordos",
    description: "Consultar acordos e seus estados atuais.",
  },
  "payments:write": {
    label: "Criar pagamentos",
    description: "Iniciar operações de pagamento autorizadas.",
  },
  "payments:read": {
    label: "Ler pagamentos",
    description: "Consultar cobranças e confirmações PIX.",
  },
  "webhooks:manage": {
    label: "Gerenciar webhooks",
    description: "Configurar destinos e entregas de webhook.",
  },
};

function apiKeyError(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message;
  }

  return "Não foi possível carregar as chaves de integração.";
}

function displayTimestamp(value: string | null, fallback: string): string {
  if (!value) {
    return fallback;
  }

  return new Intl.DateTimeFormat("pt-BR", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "UTC",
  }).format(new Date(value));
}

function statusLabel(status: OrganizationApiKey["status"]): string {
  if (status === "ACTIVE") {
    return "Ativa";
  }

  if (status === "EXPIRED") {
    return "Expirada";
  }

  return "Revogada";
}

export function ApiKeyPanel() {
  const [state, setState] = useState<ApiKeyLoadState>({ status: "loading" });
  const [isCreateOpen, setIsCreateOpen] = useState(false);
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<ApiKeyScope[]>([]);
  const [expiresOn, setExpiresOn] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [isCreating, setIsCreating] = useState(false);
  const [revealedKey, setRevealedKey] = useState<RevealedKey | null>(null);
  const [pendingAction, setPendingAction] = useState<PendingAction | null>(null);
  const [overlapSeconds, setOverlapSeconds] = useState("86400");
  const [actionError, setActionError] = useState<string | null>(null);
  const [isApplyingAction, setIsApplyingAction] = useState(false);

  useEffect(() => {
    let isMounted = true;

    void organizationApi
      .apiKeys()
      .then(({ api_keys: apiKeys }) => {
        if (isMounted) {
          setState({ status: "ready", apiKeys });
        }
      })
      .catch((error: unknown) => {
        if (isMounted) {
          setState({ status: "error", message: apiKeyError(error) });
        }
      });

    return () => {
      isMounted = false;
    };
  }, []);

  const activeKeyCount =
    state.status === "ready"
      ? state.apiKeys.filter((apiKey) => apiKey.status === "ACTIVE").length
      : 0;
  const activeKeyLimitReached = activeKeyCount >= 2;

  function toggleScope(scope: ApiKeyScope) {
    setScopes((currentScopes) =>
      currentScopes.includes(scope)
        ? currentScopes.filter((currentScope) => currentScope !== scope)
        : [...currentScopes, scope],
    );
  }

  function closeCreateForm() {
    setIsCreateOpen(false);
    setFormError(null);
  }

  function closeReveal() {
    setRevealedKey(null);
    setName("");
    setScopes([]);
    setExpiresOn("");
  }

  async function createApiKey(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmedName = name.trim();

    if (!trimmedName) {
      setFormError("Dê um nome para identificar esta chave.");
      return;
    }

    if (!scopes.length) {
      setFormError("Selecione pelo menos um escopo de acesso.");
      return;
    }

    setFormError(null);
    setIsCreating(true);

    try {
      const response = await organizationApi.createApiKey({
        name: trimmedName,
        scopes,
        ...(expiresOn ? { expires_at: `${expiresOn}T23:59:59Z` } : {}),
      });
      setState((currentState) => {
        if (currentState.status !== "ready") {
          return currentState;
        }

        return { status: "ready", apiKeys: [response.api_key, ...currentState.apiKeys] };
      });
      setIsCreateOpen(false);
      setRevealedKey({ apiKey: response.api_key, secret: response.secret });
    } catch (error) {
      setFormError(apiKeyError(error));
    } finally {
      setIsCreating(false);
    }
  }

  function openRotation(apiKey: OrganizationApiKey) {
    setPendingAction({ apiKey, type: "rotate" });
    setOverlapSeconds("86400");
    setActionError(null);
  }

  function openRevocation(apiKey: OrganizationApiKey) {
    setPendingAction({ apiKey, type: "revoke" });
    setActionError(null);
  }

  function closePendingAction() {
    setPendingAction(null);
    setActionError(null);
  }

  async function confirmPendingAction() {
    if (!pendingAction) {
      return;
    }

    setActionError(null);
    setIsApplyingAction(true);

    try {
      if (pendingAction.type === "rotate") {
        const response = await organizationApi.rotateApiKey(
          pendingAction.apiKey.id,
          Number(overlapSeconds),
        );
        setState((currentState) => {
          if (currentState.status !== "ready") {
            return currentState;
          }

          return {
            status: "ready",
            apiKeys: [
              response.api_key,
              ...currentState.apiKeys.map((apiKey) =>
                apiKey.id === response.previous_api_key?.id ? response.previous_api_key : apiKey,
              ),
            ],
          };
        });
        setRevealedKey({ apiKey: response.api_key, secret: response.secret });
      } else {
        const response = await organizationApi.revokeApiKey(pendingAction.apiKey.id);
        setState((currentState) => {
          if (currentState.status !== "ready") {
            return currentState;
          }

          return {
            status: "ready",
            apiKeys: currentState.apiKeys.map((apiKey) =>
              apiKey.id === response.api_key.id ? response.api_key : apiKey,
            ),
          };
        });
      }
      setPendingAction(null);
    } catch (error) {
      setActionError(apiKeyError(error));
    } finally {
      setIsApplyingAction(false);
    }
  }

  return (
    <section className="integration-ledger" aria-labelledby="api-key-title">
      <div className="integration-ledger-heading">
        <div>
          <p className="eyebrow">ACESSO PROGRAMÁTICO</p>
          <h2 id="api-key-title">Chaves de integração</h2>
        </div>
        <div className="integration-heading-actions">
          <span>Somente proprietários</span>
          <button
            className="secondary-action"
            type="button"
            onClick={() => setIsCreateOpen(true)}
            disabled={state.status !== "ready" || activeKeyLimitReached}
            aria-describedby={activeKeyLimitReached ? "api-key-limit" : undefined}
          >
            Criar chave de integração
          </button>
        </div>
      </div>

      {state.status === "loading" ? (
        <p className="empty-integration">Carregando as chaves desta organização…</p>
      ) : null}

      {state.status === "error" ? (
        <p className="workspace-error" role="alert">
          {state.message}
        </p>
      ) : null}

      {state.status === "ready" && state.apiKeys.length === 0 ? (
        <p className="empty-integration">Nenhuma chave ativa foi criada para esta organização.</p>
      ) : null}

      {state.status === "ready" && activeKeyLimitReached ? (
        <p id="api-key-limit" className="api-key-limit">
          Limite de duas chaves ativas atingido. Revogue uma chave antes de criar ou rotacionar
          outra.
        </p>
      ) : null}

      {state.status === "ready" && state.apiKeys.length > 0 ? (
        <div className="integration-table-wrap">
          <table className="integration-table">
            <thead>
              <tr>
                <th scope="col">Chave</th>
                <th scope="col">Escopos</th>
                <th scope="col">Expira</th>
                <th scope="col">Último uso</th>
                <th scope="col">Ações</th>
              </tr>
            </thead>
            <tbody>
              {state.apiKeys.map((apiKey) => (
                <tr key={apiKey.id}>
                  <th scope="row">
                    <strong>{apiKey.name}</strong>
                    <span className="api-key-identifier">
                      <code>{apiKey.prefix}</code>
                      <small
                        className={`api-key-status api-key-status-${apiKey.status.toLowerCase()}`}
                      >
                        {statusLabel(apiKey.status)}
                      </small>
                    </span>
                  </th>
                  <td>
                    <ul className="scope-list" aria-label={`Escopos de ${apiKey.name}`}>
                      {apiKey.scopes.map((scope) => (
                        <li key={scope}>{scope}</li>
                      ))}
                    </ul>
                  </td>
                  <td>{displayTimestamp(apiKey.expires_at, "Sem expiração")}</td>
                  <td>
                    <span>{displayTimestamp(apiKey.last_used_at, "Ainda não usada")}</span>
                    {apiKey.last_used_ip ? <code>{apiKey.last_used_ip}</code> : null}
                  </td>
                  <td>
                    {apiKey.status === "ACTIVE" ? (
                      <div className="api-key-actions">
                        <button
                          className="text-action"
                          type="button"
                          aria-label={`Rotacionar ${apiKey.name}`}
                          onClick={() => openRotation(apiKey)}
                          disabled={activeKeyLimitReached}
                        >
                          Rotacionar
                        </button>
                        <button
                          className="danger-action"
                          type="button"
                          aria-label={`Revogar ${apiKey.name}`}
                          onClick={() => openRevocation(apiKey)}
                        >
                          Revogar
                        </button>
                      </div>
                    ) : (
                      <span className="api-key-revoked">Sem ações</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {isCreateOpen ? (
        <section
          className="integration-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="create-api-key-title"
        >
          <form className="integration-form" onSubmit={(event) => void createApiKey(event)}>
            <div className="integration-dialog-heading">
              <div>
                <p className="eyebrow">NOVA CREDENCIAL</p>
                <h3 id="create-api-key-title">Configure o menor acesso necessário.</h3>
              </div>
              <button className="text-action" type="button" onClick={closeCreateForm}>
                Cancelar
              </button>
            </div>

            <label>
              Nome da chave
              <input
                name="api-key-name"
                value={name}
                onChange={(event) => setName(event.target.value)}
                maxLength={80}
                autoComplete="off"
              />
            </label>

            <fieldset>
              <legend>Escopos permitidos</legend>
              <p>Selecione apenas as operações que sua integração precisa executar.</p>
              <div className="scope-options">
                {apiKeyScopes.map((scope) => (
                  <label key={scope} className="scope-option">
                    <input
                      type="checkbox"
                      checked={scopes.includes(scope)}
                      onChange={() => toggleScope(scope)}
                    />
                    <span>
                      <strong>{scopeLabels[scope].label}</strong>
                      <small>{scopeLabels[scope].description}</small>
                    </span>
                  </label>
                ))}
              </div>
            </fieldset>

            <label>
              Expira em
              <input
                type="date"
                value={expiresOn}
                onChange={(event) => setExpiresOn(event.target.value)}
              />
              <small>Opcional. A chave expira às 23:59 UTC da data escolhida.</small>
            </label>

            {formError ? (
              <p className="form-feedback form-feedback-error" role="alert">
                {formError}
              </p>
            ) : null}

            <div className="integration-form-actions">
              <button className="primary-action" type="submit" disabled={isCreating}>
                {isCreating ? "Gerando…" : "Gerar chave"}
              </button>
            </div>
          </form>
        </section>
      ) : null}

      {revealedKey ? (
        <section
          className="secret-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="revealed-api-key-title"
        >
          <p className="eyebrow">CREDENCIAL GERADA</p>
          <h3 id="revealed-api-key-title">Guarde sua nova chave</h3>
          <p>
            Copie este valor agora. Por segurança, ele não será exibido novamente após fechar este
            aviso.
          </p>
          <code className="secret-value">{revealedKey.secret}</code>
          <p className="secret-key-reference">
            Prefixo auditável: <code>{revealedKey.apiKey.prefix}</code>
          </p>
          <button className="primary-action" type="button" onClick={closeReveal}>
            Entendi e fechei
          </button>
        </section>
      ) : null}

      {pendingAction ? (
        <section
          className="integration-dialog integration-action-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="api-key-action-title"
        >
          {pendingAction.type === "rotate" ? (
            <>
              <p className="eyebrow">ROTAÇÃO SEGURA</p>
              <h3 id="api-key-action-title">Rotacionar chave</h3>
              <p>
                A nova chave será exibida uma única vez. A credencial atual continuará válida
                durante a janela selecionada para evitar indisponibilidade na integração.
              </p>
              <label className="overlap-control">
                Sobreposição da chave atual
                <select
                  value={overlapSeconds}
                  onChange={(event) => setOverlapSeconds(event.target.value)}
                >
                  <option value="3600">1 hora</option>
                  <option value="86400">24 horas (recomendado)</option>
                </select>
              </label>
            </>
          ) : (
            <>
              <p className="eyebrow">REVOGAÇÃO IMEDIATA</p>
              <h3 id="api-key-action-title">Revogar chave</h3>
              <p>
                <strong>{pendingAction.apiKey.name}</strong> deixará de autenticar qualquer chamada
                imediatamente. Esta ação não pode ser desfeita.
              </p>
            </>
          )}

          {actionError ? (
            <p className="form-feedback form-feedback-error" role="alert">
              {actionError}
            </p>
          ) : null}

          <div className="integration-form-actions">
            <button className="text-action" type="button" onClick={closePendingAction}>
              Cancelar
            </button>
            <button
              className={pendingAction.type === "revoke" ? "danger-action" : "primary-action"}
              type="button"
              onClick={() => void confirmPendingAction()}
              disabled={isApplyingAction}
            >
              {isApplyingAction
                ? "Aplicando…"
                : pendingAction.type === "rotate"
                  ? "Confirmar rotação"
                  : "Revogar chave"}
            </button>
          </div>
        </section>
      ) : null}
    </section>
  );
}

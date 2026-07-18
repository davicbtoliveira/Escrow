import { type FormEvent, useEffect, useState } from "react";
import {
  ApiError,
  organizationApi,
  type WebhookDelivery,
  type WebhookDeliveryStatus,
  type WebhookEndpoint,
} from "../../lib/api";

type LoadState =
  | { status: "loading" }
  | { status: "ready"; endpoints: WebhookEndpoint[]; deliveries: WebhookDelivery[] }
  | { status: "error"; message: string };

const deliveryStatusLabels: Record<WebhookDeliveryStatus, string> = {
  PENDING: "Pendente",
  RETRYING: "Reagendada",
  DELIVERED: "Entregue",
  FAILED: "Falhou",
};

function webhookError(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message;
  }

  return "Não foi possível carregar os webhooks desta organização.";
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

export function WebhookPanel() {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [isCreateOpen, setIsCreateOpen] = useState(false);
  const [url, setUrl] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [isCreating, setIsCreating] = useState(false);
  const [revealedSecret, setRevealedSecret] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busyDeliveryId, setBusyDeliveryId] = useState<string | null>(null);
  const [busyRotationId, setBusyRotationId] = useState<string | null>(null);

  useEffect(() => {
    let isMounted = true;

    void Promise.all([organizationApi.webhookEndpoints(), organizationApi.webhookDeliveries()])
      .then(([{ webhook_endpoints: endpoints }, { webhook_deliveries: deliveries }]) => {
        if (isMounted) {
          setState({ status: "ready", endpoints, deliveries });
        }
      })
      .catch((error: unknown) => {
        if (isMounted) {
          setState({ status: "error", message: webhookError(error) });
        }
      });

    return () => {
      isMounted = false;
    };
  }, []);

  function closeCreateForm() {
    setIsCreateOpen(false);
    setFormError(null);
  }

  function closeReveal() {
    setRevealedSecret(null);
    setUrl("");
  }

  async function createEndpoint(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmedUrl = url.trim();

    if (!trimmedUrl) {
      setFormError("Informe a URL HTTPS pública do seu endpoint.");
      return;
    }

    setFormError(null);
    setIsCreating(true);

    try {
      const response = await organizationApi.createWebhookEndpoint(trimmedUrl);
      setState((currentState) =>
        currentState.status === "ready"
          ? {
              status: "ready",
              endpoints: [...currentState.endpoints, response.webhook_endpoint],
              deliveries: currentState.deliveries,
            }
          : currentState,
      );
      setIsCreateOpen(false);
      setRevealedSecret(response.secret);
    } catch (error) {
      setFormError(webhookError(error));
    } finally {
      setIsCreating(false);
    }
  }

  async function rotateSecret(endpoint: WebhookEndpoint) {
    setActionError(null);
    setBusyRotationId(endpoint.id);

    try {
      const response = await organizationApi.rotateWebhookSecret(endpoint.id, 3600);
      setState((currentState) =>
        currentState.status === "ready"
          ? {
              status: "ready",
              endpoints: currentState.endpoints.map((item) =>
                item.id === endpoint.id ? response.webhook_endpoint : item,
              ),
              deliveries: currentState.deliveries,
            }
          : currentState,
      );
      setRevealedSecret(response.secret);
    } catch (error) {
      setActionError(webhookError(error));
    } finally {
      setBusyRotationId(null);
    }
  }

  async function replayDelivery(delivery: WebhookDelivery) {
    setActionError(null);
    setBusyDeliveryId(delivery.id);

    try {
      const response = await organizationApi.replayWebhookDelivery(delivery.id);
      setState((currentState) =>
        currentState.status === "ready"
          ? {
              status: "ready",
              endpoints: currentState.endpoints,
              deliveries: currentState.deliveries.map((item) =>
                item.id === delivery.id ? response.webhook_delivery : item,
              ),
            }
          : currentState,
      );
    } catch (error) {
      setActionError(webhookError(error));
    } finally {
      setBusyDeliveryId(null);
    }
  }

  return (
    <section className="integration-ledger" aria-labelledby="webhook-title">
      <div className="integration-ledger-heading">
        <div>
          <p className="eyebrow">NOTIFICAÇÕES ASSINADAS</p>
          <h2 id="webhook-title">Webhooks da integração</h2>
        </div>
        <div className="integration-heading-actions">
          <span>Somente proprietários</span>
          <button
            className="secondary-action"
            type="button"
            onClick={() => setIsCreateOpen(true)}
            disabled={state.status !== "ready"}
          >
            Configurar endpoint
          </button>
        </div>
      </div>

      {state.status === "loading" ? (
        <p className="empty-integration">Carregando os webhooks desta organização…</p>
      ) : null}

      {state.status === "error" ? (
        <p className="workspace-error" role="alert">
          {state.message}
        </p>
      ) : null}

      {actionError ? (
        <p className="form-feedback form-feedback-error" role="alert">
          {actionError}
        </p>
      ) : null}

      {state.status === "ready" && state.endpoints.length === 0 ? (
        <p className="empty-integration">
          Nenhum endpoint configurado. Cadastre uma URL HTTPS pública para receber eventos
          assinados.
        </p>
      ) : null}

      {state.status === "ready" && state.endpoints.length > 0 ? (
        <div className="integration-table-wrap">
          <table className="integration-table">
            <thead>
              <tr>
                <th scope="col">Endpoint</th>
                <th scope="col">Rotação</th>
                <th scope="col">Ações</th>
              </tr>
            </thead>
            <tbody>
              {state.endpoints.map((endpoint) => (
                <tr key={endpoint.id}>
                  <th scope="row">
                    <strong>{endpoint.url}</strong>
                  </th>
                  <td>
                    {endpoint.previous_secret_expires_at
                      ? `Segredo anterior válido até ${displayTimestamp(endpoint.previous_secret_expires_at, "")}`
                      : "Sem sobreposição ativa"}
                  </td>
                  <td>
                    <button
                      className="text-action"
                      type="button"
                      onClick={() => void rotateSecret(endpoint)}
                      disabled={busyRotationId === endpoint.id}
                    >
                      {busyRotationId === endpoint.id ? "Rotacionando…" : "Rotacionar segredo"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {state.status === "ready" && state.endpoints.length > 0 ? (
        <div className="integration-table-wrap">
          <table className="integration-table" aria-label="Entregas de webhook">
            <thead>
              <tr>
                <th scope="col">Evento</th>
                <th scope="col">Status</th>
                <th scope="col">Tentativas</th>
                <th scope="col">Próxima tentativa</th>
                <th scope="col">Ações</th>
              </tr>
            </thead>
            <tbody>
              {state.deliveries.map((delivery) => (
                <tr key={delivery.id}>
                  <th scope="row">
                    <strong>{delivery.event_type}</strong>
                    <span className="api-key-identifier">
                      <code>#{delivery.sequence}</code>
                      {delivery.last_error ? <small>{delivery.last_error}</small> : null}
                    </span>
                  </th>
                  <td>{deliveryStatusLabels[delivery.status]}</td>
                  <td>{delivery.attempts}</td>
                  <td>{displayTimestamp(delivery.next_attempt_at, "—")}</td>
                  <td>
                    {delivery.status === "FAILED" ? (
                      <button
                        className="text-action"
                        type="button"
                        onClick={() => void replayDelivery(delivery)}
                        disabled={busyDeliveryId === delivery.id}
                      >
                        {busyDeliveryId === delivery.id ? "Reenviando…" : "Reenviar"}
                      </button>
                    ) : (
                      <span className="api-key-revoked">—</span>
                    )}
                  </td>
                </tr>
              ))}
              {state.deliveries.length === 0 ? (
                <tr>
                  <td colSpan={5}>Nenhuma entrega registrada ainda.</td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      ) : null}

      {isCreateOpen ? (
        <section
          className="integration-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="create-webhook-title"
        >
          <form className="integration-form" onSubmit={(event) => void createEndpoint(event)}>
            <div className="integration-dialog-heading">
              <div>
                <p className="eyebrow">NOVO DESTINO</p>
                <h3 id="create-webhook-title">Receba eventos assinados por HMAC.</h3>
              </div>
              <button className="text-action" type="button" onClick={closeCreateForm}>
                Cancelar
              </button>
            </div>

            <label>
              URL HTTPS pública
              <input
                name="webhook-url"
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                maxLength={2048}
                autoComplete="off"
                placeholder="https://sua-loja.example/webhooks/escrow"
              />
              <small>
                Apenas HTTPS na porta 443 com endereço IP público. Redirecionamentos não são
                seguidos.
              </small>
            </label>

            {formError ? (
              <p className="form-feedback form-feedback-error" role="alert">
                {formError}
              </p>
            ) : null}

            <div className="integration-form-actions">
              <button className="primary-action" type="submit" disabled={isCreating}>
                {isCreating ? "Validando…" : "Salvar endpoint"}
              </button>
            </div>
          </form>
        </section>
      ) : null}

      {revealedSecret ? (
        <section
          className="secret-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="revealed-webhook-title"
        >
          <p className="eyebrow">SEGREDO GERADO</p>
          <h3 id="revealed-webhook-title">Guarde o segredo de assinatura</h3>
          <p>
            Copie este valor agora. Por segurança, ele não será exibido novamente após fechar este
            aviso.
          </p>
          <code className="secret-value">{revealedSecret}</code>
          <button className="primary-action" type="button" onClick={closeReveal}>
            Entendi e fechei
          </button>
        </section>
      ) : null}
    </section>
  );
}

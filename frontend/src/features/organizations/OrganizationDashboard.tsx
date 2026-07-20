import { useEffect, useState } from "react";
import { ApiError, type CurrentOrganization, organizationApi } from "../../lib/api";
import { ApiKeyPanel } from "../integrations/ApiKeyPanel";
import { WebhookPanel } from "../integrations/WebhookPanel";
import { approximateAmount, type Currency, type DisplayExchangeRate, formatMoney } from "./money";

type OrganizationDashboardProps = {
  onLogout: () => void;
  onReturnToLogin: () => void;
};

type LoadState =
  | { status: "loading" }
  | { status: "ready"; organization: CurrentOrganization }
  | { status: "error"; message: string };

function displayDate(value: string): string {
  return new Intl.DateTimeFormat("pt-BR", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "UTC",
  }).format(new Date(value));
}

function dashboardError(error: unknown): string {
  if (error instanceof ApiError && error.status === 401) {
    return "Sua sessão não está ativa. Entre novamente para abrir os dados da organização.";
  }

  if (error instanceof ApiError) {
    return error.message;
  }

  return "Não foi possível carregar o espaço da organização.";
}

export function OrganizationDashboard({ onLogout, onReturnToLogin }: OrganizationDashboardProps) {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [isLoggingOut, setIsLoggingOut] = useState(false);
  const [showApproximate, setShowApproximate] = useState(false);

  useEffect(() => {
    let isMounted = true;

    void organizationApi
      .currentOrganization()
      .then((organization) => {
        if (isMounted) {
          setState({ status: "ready", organization });
        }
      })
      .catch((error: unknown) => {
        if (isMounted) {
          setState({ status: "error", message: dashboardError(error) });
        }
      });

    return () => {
      isMounted = false;
    };
  }, []);

  async function logOut() {
    setIsLoggingOut(true);
    try {
      await organizationApi.logout();
      onLogout();
    } catch (error) {
      setState({ status: "error", message: dashboardError(error) });
    } finally {
      setIsLoggingOut(false);
    }
  }

  if (state.status === "loading") {
    return (
      <main className="workspace-shell workspace-loading" aria-live="polite">
        <p className="eyebrow">ESPAÇO DA ORGANIZAÇÃO</p>
        <h1>Carregando sua custódia…</h1>
      </main>
    );
  }

  if (state.status === "error") {
    return (
      <main className="workspace-shell workspace-loading">
        <p className="eyebrow">ACESSO À ORGANIZAÇÃO</p>
        <h1>Não abrimos este espaço.</h1>
        <p className="workspace-error" role="alert">
          {state.message}
        </p>
        <button className="primary-action workspace-return" type="button" onClick={onReturnToLogin}>
          Voltar para entrar
        </button>
      </main>
    );
  }

  const {
    balances,
    exchange_rates: exchangeRates,
    membership,
    organization,
    upcoming_releases: upcomingReleases,
  } = state.organization;

  return (
    <main className="workspace-shell">
      <a className="skip-link" href="#organization-overview">
        Ir para o resumo financeiro
      </a>

      <header className="workspace-topbar">
        <div className="brand-lockup">
          <span className="brand-mark" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
          <span>ESCROW</span>
        </div>
        <div className="workspace-account">
          <span>SESSÃO DA ORGANIZAÇÃO</span>
          <button type="button" onClick={() => void logOut()} disabled={isLoggingOut}>
            {isLoggingOut ? "Saindo…" : "Sair"}
          </button>
        </div>
      </header>

      <section
        id="organization-overview"
        className="organization-overview"
        aria-labelledby="organization-title"
      >
        <div className="organization-heading">
          <p className="eyebrow">ORGANIZAÇÃO ATUAL · ESCOPO ISOLADO</p>
          <h1 id="organization-title">{organization.name}</h1>
          <p>
            {organization.document_masked
              ? `Documento ${organization.document_masked}`
              : "Dados financeiros desta organização"}
          </p>
        </div>

        <aside className="membership-card" aria-labelledby="membership-title">
          <p id="membership-title">SEU ACESSO</p>
          <strong>{membership.role}</strong>
          <span>Dados somente desta organização</span>
        </aside>
      </section>

      <div className="balance-toolbar">
        <button
          type="button"
          className="balance-toggle"
          aria-pressed={showApproximate}
          onClick={() => setShowApproximate((current) => !current)}
        >
          {showApproximate ? "Ocultar conversão simulada" : "Exibir conversão simulada"}
        </button>
        {showApproximate ? (
          <p className="balance-toggle-note">
            Valores aproximados com taxa simulada. A conversão nunca altera acordos, lançamentos ou
            saldos.
          </p>
        ) : null}
      </div>

      <section className="balance-ledger" aria-label="Saldos da organização por moeda">
        <BalanceBand
          label="Em custódia"
          tone="held"
          amounts={[
            { currency: "BRL", amountMinor: balances.held_brl_minor },
            { currency: "USD", amountMinor: balances.held_usd_minor },
          ]}
          exchangeRates={exchangeRates}
          showApproximate={showApproximate}
        />
        <BalanceBand
          label="Disponível"
          tone="available"
          amounts={[
            { currency: "BRL", amountMinor: balances.available_brl_minor },
            { currency: "USD", amountMinor: balances.available_usd_minor },
          ]}
          exchangeRates={exchangeRates}
          showApproximate={showApproximate}
        />
        <BalanceBand
          label="Taxas da plataforma"
          tone="fees"
          amounts={[
            { currency: "BRL", amountMinor: balances.fee_brl_minor },
            { currency: "USD", amountMinor: balances.fee_usd_minor },
          ]}
          exchangeRates={exchangeRates}
          showApproximate={showApproximate}
        />
      </section>

      <section className="release-ledger" aria-labelledby="release-ledger-title">
        <div className="release-ledger-heading">
          <div>
            <p className="eyebrow">PREVISÃO OPERACIONAL</p>
            <h2 id="release-ledger-title">Próximas liberações</h2>
          </div>
          <span>{upcomingReleases.length} agendada(s)</span>
        </div>

        {upcomingReleases.length ? (
          <ol className="release-list">
            {upcomingReleases.map((release) => (
              <li key={release.id}>
                <span className="release-mark" aria-hidden="true" />
                <div>
                  <strong>{release.id}</strong>
                  <span>Disponível em {displayDate(release.release_at)}</span>
                  <span>
                    Bruto {formatMoney(release.gross_minor, release.currency)} · Taxa{" "}
                    {formatMoney(release.fee_minor, release.currency)} · Líquido{" "}
                    {formatMoney(release.net_minor, release.currency)}
                  </span>
                </div>
                <output>{formatMoney(release.net_minor, release.currency)}</output>
              </li>
            ))}
          </ol>
        ) : (
          <p className="empty-release">Nenhuma liberação está prevista para esta organização.</p>
        )}
      </section>

      {membership.role === "OWNER" ? (
        <>
          <ApiKeyPanel />
          <WebhookPanel />
        </>
      ) : null}
    </main>
  );
}

function BalanceBand({
  amounts,
  exchangeRates,
  label,
  showApproximate,
  tone,
}: {
  amounts: { currency: Currency; amountMinor: number }[];
  exchangeRates: DisplayExchangeRate[];
  label: string;
  showApproximate: boolean;
  tone: "available" | "fees" | "held";
}) {
  return (
    <section className={`balance-band balance-band-${tone}`} aria-label={label}>
      <div>
        <p>{label}</p>
        <span>
          {tone === "held"
            ? "Valores protegidos até a liberação"
            : tone === "available"
              ? "Valores já liberados para a organização"
              : "Taxas já descontadas das liberações"}
        </span>
      </div>
      <dl>
        {amounts.map(({ currency, amountMinor }) => {
          const approximate = showApproximate
            ? approximateAmount(amountMinor, currency, exchangeRates)
            : null;
          return (
            <div key={currency}>
              <dt>{currency}</dt>
              <dd>
                {formatMoney(amountMinor, currency)}
                {approximate ? (
                  <span className="balance-approx">
                    ≈ {formatMoney(approximate.amountMinor, approximate.currency)}
                  </span>
                ) : null}
              </dd>
            </div>
          );
        })}
      </dl>
    </section>
  );
}

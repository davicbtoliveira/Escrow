import { useEffect, useState } from "react";
import { ApiError, type CurrentOrganization, organizationApi } from "../../lib/api";

type OrganizationDashboardProps = {
  onLogout: () => void;
  onReturnToLogin: () => void;
};

type LoadState =
  | { status: "loading" }
  | { status: "ready"; organization: CurrentOrganization }
  | { status: "error"; message: string };

function displayMoney(amountMinor: number): string {
  return new Intl.NumberFormat("pt-BR", {
    style: "currency",
    currency: "BRL",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(amountMinor / 100);
}

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

      <section className="balance-ledger" aria-label="Saldos da organização em BRL">
        <BalanceBand label="Em custódia" tone="held" amountMinor={balances.held_brl_minor} />
        <BalanceBand
          label="Disponível"
          tone="available"
          amountMinor={balances.available_brl_minor}
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
                </div>
                <output>
                  {new Intl.NumberFormat("pt-BR", {
                    style: "currency",
                    currency: release.currency,
                  }).format(release.amount_minor / 100)}
                </output>
              </li>
            ))}
          </ol>
        ) : (
          <p className="empty-release">Nenhuma liberação está prevista para esta organização.</p>
        )}
      </section>
    </main>
  );
}

function BalanceBand({
  amountMinor,
  label,
  tone,
}: {
  amountMinor: number;
  label: string;
  tone: "available" | "held";
}) {
  return (
    <section className={`balance-band balance-band-${tone}`} aria-label={label}>
      <div>
        <p>{label}</p>
        <span>
          {tone === "held"
            ? "Valores protegidos até a liberação"
            : "Valores já liberados para a organização"}
        </span>
      </div>
      <dl>
        <div>
          <dt>BRL</dt>
          <dd>{displayMoney(amountMinor)}</dd>
        </div>
      </dl>
    </section>
  );
}

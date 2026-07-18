import { useCallback, useEffect, useState } from "react";
import { type AccessRoute, AccessScreen } from "./features/access/AccessScreen";
import { CheckoutScreen } from "./features/checkout/CheckoutScreen";
import { OrganizationDashboard } from "./features/organizations/OrganizationDashboard";
import "./styles.css";

type ReadinessState = "checking" | "ready" | "degraded";
type CheckoutRoute = { kind: "checkout"; token: string };
type AppRoute = "/" | "/dashboard" | AccessRoute | CheckoutRoute;
type NavigableRoute = Exclude<AppRoute, CheckoutRoute>;

type ReadinessCopy = {
  detail: string;
  label: string;
};

const readinessCopy: Record<ReadinessState, ReadinessCopy> = {
  checking: {
    label: "Verificando operação",
    detail: "Consultando a prontidão dos serviços que sustentam a custódia.",
  },
  ready: {
    label: "Operação normal",
    detail: "A API está pronta para receber novos acordos de custódia.",
  },
  degraded: {
    label: "Atenção operacional",
    detail: "Não foi possível confirmar a prontidão. Verifique o serviço e tente novamente.",
  },
};

function hasReadyStatus(payload: unknown): payload is { status: "ready" } {
  return (
    typeof payload === "object" &&
    payload !== null &&
    "status" in payload &&
    payload.status === "ready"
  );
}

function routeFromPath(pathname: string): AppRoute {
  const normalizedPath = pathname === "/" ? pathname : pathname.replace(/\/+$/, "");
  const checkoutMatch = normalizedPath.match(/^\/checkout\/([^/]+)$/);

  if (checkoutMatch) {
    try {
      return { kind: "checkout", token: decodeURIComponent(checkoutMatch[1]) };
    } catch {
      return "/";
    }
  }

  if (
    normalizedPath === "/login" ||
    normalizedPath === "/recuperar" ||
    normalizedPath === "/redefinir-senha" ||
    normalizedPath === "/registro" ||
    normalizedPath === "/dashboard"
  ) {
    return normalizedPath;
  }

  return "/";
}

export default function App() {
  const [route, setRoute] = useState<AppRoute>(() => routeFromPath(window.location.pathname));

  useEffect(() => {
    const handleHistoryNavigation = () => setRoute(routeFromPath(window.location.pathname));
    window.addEventListener("popstate", handleHistoryNavigation);

    return () => window.removeEventListener("popstate", handleHistoryNavigation);
  }, []);

  const navigate = useCallback((nextRoute: NavigableRoute) => {
    window.history.pushState({}, "", nextRoute);
    setRoute(nextRoute);
  }, []);

  if (typeof route === "object") {
    return <CheckoutScreen token={route.token} />;
  }

  if (route === "/dashboard") {
    return (
      <OrganizationDashboard
        onLogout={() => navigate("/login")}
        onReturnToLogin={() => navigate("/login")}
      />
    );
  }

  if (
    route === "/registro" ||
    route === "/login" ||
    route === "/recuperar" ||
    route === "/redefinir-senha"
  ) {
    return (
      <AccessScreen
        mode={route}
        onAuthenticated={() => navigate("/dashboard")}
        onNavigate={navigate}
      />
    );
  }

  return <ReadinessLanding onNavigate={navigate} />;
}

function ReadinessLanding({ onNavigate }: { onNavigate: (route: NavigableRoute) => void }) {
  const [readiness, setReadiness] = useState<ReadinessState>("checking");

  const checkReadiness = useCallback(async () => {
    setReadiness("checking");

    try {
      const response = await fetch("/health/ready/", {
        headers: { Accept: "application/json" },
      });
      const payload: unknown = await response.json();

      if (!response.ok || !hasReadyStatus(payload)) {
        throw new Error("Readiness check did not report a healthy service.");
      }

      setReadiness("ready");
    } catch {
      setReadiness("degraded");
    }
  }, []);

  useEffect(() => {
    void checkReadiness();
  }, [checkReadiness]);

  const status = readinessCopy[readiness];

  return (
    <main className="app-shell">
      <a className="skip-link" href="#operation-status">
        Ir para o estado operacional
      </a>

      <header className="topbar">
        <div className="brand-lockup">
          <span className="brand-mark" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
          <span>ESCROW</span>
        </div>
        <div className="topbar-actions">
          <p className="environment-label">AMBIENTE LOCAL · SIMULAÇÃO</p>
          <button type="button" className="topbar-login" onClick={() => onNavigate("/login")}>
            Entrar
          </button>
        </div>
      </header>

      <section className="control-room" aria-labelledby="shell-title">
        <div className="introduction">
          <p className="eyebrow">CUSTÓDIA OPERACIONAL</p>
          <h1 id="shell-title">Dinheiro em trânsito, sinais sob controle.</h1>
          <p className="lede">
            O console de operação acompanha cada acordo entre pagamento, análise e liberação. Comece
            confirmando que a infraestrutura está pronta.
          </p>

          <section className="custody-rail" aria-labelledby="custody-rail-title">
            <div className="rail-heading">
              <p id="custody-rail-title">Trilha de custódia</p>
              <span>ESTADO DO CICLO</span>
            </div>
            <ol>
              <li>
                <span className="rail-node rail-node-active" aria-hidden="true" />
                <strong>Acordo</strong>
                <small>registrado</small>
              </li>
              <li>
                <span className="rail-node" aria-hidden="true" />
                <strong>Análise</strong>
                <small>de risco</small>
              </li>
              <li>
                <span className="rail-node" aria-hidden="true" />
                <strong>Custódia</strong>
                <small>confirmada</small>
              </li>
              <li>
                <span className="rail-node" aria-hidden="true" />
                <strong>Liberação</strong>
                <small>autorizada</small>
              </li>
            </ol>
          </section>
        </div>

        <aside id="operation-status" className="status-panel" aria-labelledby="status-panel-title">
          <div className="panel-kicker">
            <span>01</span>
            <p>PRONTIDÃO DA PLATAFORMA</p>
          </div>
          <h2 id="status-panel-title">Antes de iniciar</h2>
          <p className="panel-intro">
            A custódia só aceita novos eventos quando seus serviços essenciais respondem.
          </p>

          <div
            className={`readiness-card readiness-card-${readiness}`}
            role="status"
            aria-live="polite"
            aria-atomic="true"
          >
            <span className="status-indicator" aria-hidden="true" />
            <div>
              <p>{status.label}</p>
              <span>{status.detail}</span>
            </div>
          </div>

          <dl className="status-metadata">
            <div>
              <dt>Verificação</dt>
              <dd>/health/ready/</dd>
            </div>
            <div>
              <dt>Canal</dt>
              <dd>Infraestrutura local</dd>
            </div>
          </dl>

          <button type="button" className="refresh-button" onClick={() => void checkReadiness()}>
            Verificar novamente
          </button>
        </aside>
      </section>
    </main>
  );
}

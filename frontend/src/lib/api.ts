export type ScheduledRelease = {
  id: string;
  amount_minor: number;
  currency: "BRL";
  release_at: string;
};

export type CurrentOrganization = {
  organization: {
    name: string;
    document_masked: string | null;
  };
  membership: {
    role: "OWNER" | "FINANCE" | "SUPPORT" | "VIEWER";
  };
  balances: {
    held_brl_minor: number;
    available_brl_minor: number;
  };
  upcoming_releases: ScheduledRelease[];
};

export type RegisterInput = {
  organization_name: string;
  email: string;
  password: string;
  password_confirmation: string;
};

export type PasswordRecoveryConfirmationInput = {
  uid: string;
  token: string;
  password: string;
  password_confirmation: string;
};

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function responseMessage(payload: unknown): string {
  if (!isRecord(payload)) {
    return "Não foi possível concluir esta ação agora.";
  }

  for (const field of ["detail", "message", "error"] as const) {
    const value = payload[field];
    if (typeof value === "string" && value.trim()) {
      return value;
    }
  }

  const errors = payload.errors;
  if (isRecord(errors)) {
    for (const fieldErrors of Object.values(errors)) {
      if (typeof fieldErrors === "string" && fieldErrors.trim()) {
        return fieldErrors;
      }

      if (Array.isArray(fieldErrors)) {
        const firstMessage = fieldErrors.find(
          (message): message is string => typeof message === "string" && message.trim().length > 0,
        );
        if (firstMessage) {
          return firstMessage;
        }
      }
    }
  }

  return "Não foi possível concluir esta ação agora.";
}

const csrfCookieName = "escrow_csrf";

function csrfToken(): string | undefined {
  const token = document.cookie
    .split(";")
    .map((entry) => entry.trim())
    .find((entry) => entry.startsWith(`${csrfCookieName}=`))
    ?.slice(csrfCookieName.length + 1);

  return token ? decodeURIComponent(token) : undefined;
}

async function ensureCsrfToken(): Promise<string> {
  const existingToken = csrfToken();
  if (existingToken) {
    return existingToken;
  }

  const response = await fetch("/api/v1/auth/csrf/", {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  });

  if (!response.ok) {
    throw new ApiError(response.status, "Não foi possível preparar uma sessão segura.");
  }

  const token = csrfToken();
  if (!token) {
    throw new ApiError(0, "Não foi possível preparar uma sessão segura.");
  }

  return token;
}

async function request<T>(path: string, init: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...init,
  });
  const payload: unknown =
    response.status === 204 ? undefined : await response.json().catch(() => undefined);

  if (!response.ok) {
    throw new ApiError(response.status, responseMessage(payload));
  }

  return payload as T;
}

async function post<T>(path: string, body?: Record<string, string>): Promise<T> {
  const token = await ensureCsrfToken();
  const headers = new Headers({ Accept: "application/json", "X-CSRFToken": token });

  if (body) {
    headers.set("Content-Type", "application/json");
  }

  return request<T>(path, {
    method: "POST",
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
}

export const organizationApi = {
  register(input: RegisterInput): Promise<unknown> {
    return post("/api/v1/auth/register/", input);
  },

  login(input: Pick<RegisterInput, "email" | "password">): Promise<unknown> {
    return post("/api/v1/auth/login/", input);
  },

  logout(): Promise<unknown> {
    return post("/api/v1/auth/logout/");
  },

  recoverPassword(email: string): Promise<unknown> {
    return post("/api/v1/auth/password-recovery/", { email });
  },

  confirmPasswordRecovery(input: PasswordRecoveryConfirmationInput): Promise<unknown> {
    return post("/api/v1/auth/password-recovery/confirm/", input);
  },

  currentOrganization(): Promise<CurrentOrganization> {
    return request<CurrentOrganization>("/api/v1/organizations/current/", {
      method: "GET",
      headers: { Accept: "application/json" },
    });
  },
};

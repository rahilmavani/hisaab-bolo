export type Me = {
  merchant: { id: string; name: string };
  now: string;
  clock_overridden: boolean;
  paytm_mode: "mock" | "staging" | "production";
  demo_tools: boolean;
};

export type Payer = {
  id: string;
  display_name: string;
  email: string | null;
  phone: string | null;
  gstin: string | null;
  notes: string;
  merged_into: string | null;
  vpa: string | null;
  txn_count: number;
  total_paise: number;
  median_amount_paise: number | null;
  typical_minute: number | null;
  typical_time: string | null;
  first_paid_at: string | null;
  last_paid_at: string | null;
  median_gap_days: number | null;
  days_since_last: number | null;
  recurring: boolean;
  habit: string;
  badges: string[];
  lost: boolean;
};

export type Txn = {
  id: string;
  payer_id: string | null;
  payer_name: string | null;
  amount_paise: number;
  paid_at: string;
  source: "seed" | "paytm_link" | "paytm_qr" | "mock";
  payment_mode: string | null;
  description: string | null;
  description_by: "ai" | "merchant" | null;
  category: string | null;
  invoice_id: string | null;
  invoice_number: string | null;
  paytm_order_id: string | null;
};

export type InvoiceStatus = "draft" | "sent" | "overdue" | "paid" | "cancelled";

export type OpenInvoice = {
  id: string;
  number: string;
  payer_id: string;
  payer_name: string;
  amount_paise: number;
  due_date: string;
  grace_days: number;
  status: InvoiceStatus;
  email_to: string | null;
  link_url: string | null;
  days_overdue: number;
  last_reminded_at: string | null;
};

export type PayerProfile = Payer & {
  identifiers: {
    kind: "vpa" | "phone" | "email" | "paytm_cust_id";
    value: string;
    created_at: string;
  }[];
  monthly: { month: string; total_paise: number; txn_count: number }[];
  transactions: Txn[];
  invoices: {
    id: string;
    number: string;
    amount_paise: number;
    due_date: string;
    status: InvoiceStatus;
    link_url: string | null;
    paid_at: string | null;
    sent_at: string | null;
    created_at: string;
    recurring_id: string | null;
    due_in_days: number;
    grace_days: number;
  }[];
  recurring_suggestion?: {
    cadence: "weekly" | "monthly";
    every_days: number;
    amount_paise: number;
    suggested_amount_paise: number;
  } | null;
  recurring_schedule: {
    id: string;
    amount_paise: number;
    cadence: "weekly" | "monthly";
    anchor_date: string;
    next_due_date: string;
    grace_days: number;
    auto_remind: boolean;
    call_remind: boolean;
    active: boolean;
    email: string | null;
  } | null;
};

// label_bucket: a first point that starts mid-bucket (a 3-month chart from 31 Jul) is labelled by the
// bucket it mostly covers ("Aug").
export type TrendPoint = {
  bucket: string;
  total_paise: number;
  txn_count: number;
  label_bucket?: string;
};
export type HeatCell = {
  dow: number;
  hour: number;
  total_paise: number;
  txn_count: number;
};

export type DashboardSummary = {
  now: string;
  date: string;
  is_today: boolean;
  kpis: {
    collected_paise: number;
    payments: number;
    new_payers: number;
    repeat_paise: number;
    repeat_share: number;
    compare_day: string;
    compare_collected_paise: number;
    change_vs_last_week: number | null;
  };
  trend: TrendPoint[];
  heatmap: HeatCell[];
  peak_hours: { hour: number; total_paise: number }[];
  slow_day: {
    is_slow: boolean;
    today_paise: number;
    baseline_paise: number;
    drop_pct: number;
    weekday: string;
    as_of: string;
  } | null;
  lost_regulars: Payer[];
  recent: Txn[];
  outstanding: {
    total_paise: number;
    customers: number;
    invoices: OpenInvoice[];
    recently_paid: {
      id: string;
      number: string;
      payer_id: string;
      payer_name: string;
      amount_paise: number;
      paid_at: string;
    }[];
  };
};

export type LineItem = { name: string; qty: number; unit_price_paise: number };

export type Invoice = {
  id: string;
  number: string;
  payer_id: string;
  payer_name: string;
  payer_email: string | null;
  payer_phone: string | null;
  amount_paise: number;
  line_items: LineItem[];
  gst_taxable: boolean;
  payer_gstin: string | null;
  due_date: string;
  due_in_days: number;
  grace_days: number;
  status: InvoiceStatus;
  email_to: string | null;
  paytm_order_id: string;
  qr_order_id: string | null;
  link_id: string | null;
  link_url: string | null;
  qr_image_url: string | null;
  recurring_id: string | null;
  sent_at: string | null;
  paid_at: string | null;
  created_at: string;
  last_reminded_at: string | null;
  reminder_count: number;
  call_remind: boolean;
  last_called_at: string | null;
  last_call_status: CallStatus | null;
  last_call_outcome: CallOutcome | null;
  last_call_promised_date: string | null;
  paytm_raw?: {
    last_failure?: { RESPMSG?: string; TXNDATE?: string; STATUS?: string };
  };
  email_error?: string | null;
};

export type InvoiceDetail = Invoice & {
  merchant_name: string;
  reminders: Reminder[];
  payment: {
    id: string;
    amount_paise: number;
    paid_at: string;
    paytm_txn_id: string | null;
    payment_mode: string | null;
    source: string;
  } | null;
};

export type CallStatus = "queued" | "connected" | "no_answer" | "busy" | "failed";
export type CallOutcome = "will_pay" | "already_paid" | "dispute" | "call_later" | "no_commitment";

export type Reminder = {
  id: string;
  kind: "invoice" | "overdue";
  channel: "email" | "call";
  to_email: string | null;
  to_phone: string | null;
  sent_at: string;
  call_status: CallStatus | null;
  call_duration_s: number | null;
  call_outcome: CallOutcome | null;
  call_promised_date: string | null;
  call_transcript: { role: "agent" | "user"; en_text: string }[] | null;
  call_failure: string | null;
};

export type InvoiceList = {
  invoices: Invoice[];
  outstanding_paise: number;
  outstanding_customers: number;
};

export type PendingAction = {
  token: string;
  tool: "create_invoice" | "send_reminder" | "set_recurring";
  summary: Record<string, string | number | string[] | null>;
  expires_at: string;
};

export type AskComponent =
  | {
      type: "stat_cards";
      result_id: string;
      data: {
        items: {
          label: string;
          value?: number;
          value_paise?: number | null;
          value_text?: string;
          sub?: string | null;
        }[];
      };
    }
  | {
      type: "line_chart";
      result_id: string;
      data: { grain: "day" | "week" | "month"; points: TrendPoint[] };
    }
  | {
      type: "table";
      result_id: string;
      data: {
        kind: "payments" | "invoices";
        title: string;
        total_rows: number;
        total_paise: number | null;
        rows: Record<string, string | number | null>[];
      };
    }
  | {
      type: "payer_card";
      result_id: string;
      data: {
        payer_id: string;
        name: string;
        vpa: string | null;
        habit: string;
        badges: string[];
        window: { from: string; to: string };
        payments: number;
        total_paise: number | null;
        usual_amount_paise: number | null;
        usual_time: string | null;
        last_payment: string | null;
        last_payment_was: string | null;
      };
    }
  | { type: "invoice_card"; result_id: string; data: Record<string, unknown> }
  | { type: "confirm_action"; result_id?: string; data: PendingAction }
  | {
      type: "chips";
      result_id: string;
      data: {
        choices: { payer_id: string; label: string; sub: string | null }[];
      };
    };

export type AskResponse = {
  query_id: string;
  display: string;
  speech: string | null;
  lang: "hi-IN" | "en-IN";
  components: AskComponent[];
  follow_ups: string[];
  pending_action: PendingAction | null;
  model: string | null;
  fallback: boolean;
  latency_ms: number;
  transcript?: string;
  stt_ms?: number;
};

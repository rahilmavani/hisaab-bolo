-- Mock gateway link IDs: MOCK_LI_<n>.
create sequence if not exists public.paytm_mock_link_seq;
grant usage, select on sequence public.paytm_mock_link_seq to service_role;

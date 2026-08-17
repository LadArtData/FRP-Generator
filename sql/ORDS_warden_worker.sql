-- WARDEN — ORDS worker handlers

DECLARE
  PROCEDURE status_out(p_pattern IN VARCHAR2, p_method IN VARCHAR2) IS
  BEGIN
    ORDS.DEFINE_PARAMETER(
        p_module_name        => 'warden-hooks',
        p_pattern            => p_pattern,
        p_method             => p_method,
        p_name               => 'X-APEX-STATUS-CODE',
        p_bind_variable_name => 'status',
        p_source_type        => 'HEADER',
        p_param_type         => 'INT',
        p_access_method      => 'OUT');
  END status_out;
BEGIN
  ORDS.DEFINE_MODULE(p_module_name => 'warden-hooks', p_base_path => '/warden-hooks/', p_items_per_page => 0);

  ORDS.DEFINE_TEMPLATE(p_module_name => 'warden-hooks', p_pattern => 'jobs/pending');
  ORDS.DEFINE_HANDLER(p_module_name => 'warden-hooks', p_pattern => 'jobs/pending', p_method => 'GET',
    p_source_type => ORDS.source_type_plsql,
    p_source => q'[
      BEGIN
        iteria_ai.pkg_warden_worker.claim_pending_job(
          p_api_key => NVL(:api_key, OWA_UTIL.GET_CGI_ENV('HTTP_API_KEY')),
          p_status  => :status,
          p_json    => :response_body
        );
      END;
    ]');
  status_out('jobs/pending', 'GET');

  ORDS.DEFINE_TEMPLATE(p_module_name => 'warden-hooks', p_pattern => 'jobs');
  ORDS.DEFINE_HANDLER(p_module_name => 'warden-hooks', p_pattern => 'jobs', p_method => 'POST',
    p_source_type => ORDS.source_type_plsql,
    p_source => q'[
      BEGIN
        iteria_ai.pkg_warden_worker.enqueue_job(
          p_body   => :body_text,
          p_status => :status,
          p_json   => :response_body
        );
      END;
    ]');
  status_out('jobs', 'POST');

  ORDS.DEFINE_TEMPLATE(p_module_name => 'warden-hooks', p_pattern => 'jobs/:id/complete');
  ORDS.DEFINE_HANDLER(p_module_name => 'warden-hooks', p_pattern => 'jobs/:id/complete', p_method => 'POST',
    p_source_type => ORDS.source_type_plsql,
    p_source => q'[
      BEGIN
        iteria_ai.pkg_warden_worker.complete_job(p_job_id => :id, p_status => :status, p_json => :response_body);
      END;
    ]');
  status_out('jobs/:id/complete', 'POST');

  ORDS.DEFINE_TEMPLATE(p_module_name => 'warden-hooks', p_pattern => 'jobs/:id/fail');
  ORDS.DEFINE_HANDLER(p_module_name => 'warden-hooks', p_pattern => 'jobs/:id/fail', p_method => 'POST',
    p_source_type => ORDS.source_type_plsql,
    p_source => q'[
      BEGIN
        iteria_ai.pkg_warden_worker.fail_job(
          p_job_id => :id,
          p_body   => :body_text,
          p_status => :status,
          p_json   => :response_body
        );
      END;
    ]');
  status_out('jobs/:id/fail', 'POST');

  COMMIT;
END;
/

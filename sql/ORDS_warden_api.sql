-- WARDEN — ORDS browser-facing handlers (skeleton)

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

  ORDS.DEFINE_TEMPLATE(p_module_name => 'warden-hooks', p_pattern => 'health');
  ORDS.DEFINE_HANDLER(p_module_name => 'warden-hooks', p_pattern => 'health', p_method => 'GET',
    p_source_type => ORDS.source_type_plsql,
    p_source => q'[
      BEGIN
        :status := 200;
        OWA_UTIL.MIME_HEADER('application/json', FALSE);
        HTP.P('{"ok":true,"service":"warden"}');
      END;
    ]');
  status_out('health', 'GET');

  ORDS.DEFINE_TEMPLATE(p_module_name => 'warden-hooks', p_pattern => 'tenant/unlock');
  ORDS.DEFINE_HANDLER(p_module_name => 'warden-hooks', p_pattern => 'tenant/unlock', p_method => 'POST',
    p_source_type => ORDS.source_type_plsql,
    p_source => q'[
      BEGIN
        iteria_ai.pkg_warden_core.tenant_unlock(
          p_body    => :body_text,
          p_status  => :status,
          p_json    => :response_body
        );
      END;
    ]');
  status_out('tenant/unlock', 'POST');

  ORDS.DEFINE_TEMPLATE(p_module_name => 'warden-hooks', p_pattern => 'baseline/unlock');
  ORDS.DEFINE_HANDLER(p_module_name => 'warden-hooks', p_pattern => 'baseline/unlock', p_method => 'POST',
    p_source_type => ORDS.source_type_plsql,
    p_source => q'[
      BEGIN
        iteria_ai.pkg_warden_core.baseline_unlock(
          p_body    => :body_text,
          p_status  => :status,
          p_json    => :response_body
        );
      END;
    ]');
  status_out('baseline/unlock', 'POST');

  COMMIT;
END;
/

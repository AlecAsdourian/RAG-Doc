-- Sample data for development and testing

-- Organizations
INSERT INTO organizations (id, name, slug) VALUES
  ('00000000-0000-0000-0000-000000000001', 'Acme Corp', 'acme-corp'),
  ('00000000-0000-0000-0000-000000000002', 'Demo Org', 'demo-org');

-- Projects
INSERT INTO projects (id, organization_id, name, slug) VALUES
  ('10000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000001', 'Main Platform', 'main-platform'),
  ('10000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000001', 'Mobile App', 'mobile-app'),
  ('10000000-0000-0000-0000-000000000003', '00000000-0000-0000-0000-000000000002', 'Demo Project', 'demo-project');

-- Repositories
INSERT INTO repositories (id, project_id, name, git_url, default_branch) VALUES
  ('20000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', 'backend', 'https://github.com/acme/platform-backend', 'main'),
  ('20000000-0000-0000-0000-000000000002', '10000000-0000-0000-0000-000000000001', 'frontend', 'https://github.com/acme/platform-frontend', 'main'),
  ('20000000-0000-0000-0000-000000000003', '10000000-0000-0000-0000-000000000002', 'mobile-ios', 'https://github.com/acme/mobile-ios', 'develop');

-- Verification queries

-- 1. Check row counts
SELECT 'organizations' AS table_name, COUNT(*) AS row_count FROM organizations
UNION ALL
SELECT 'projects', COUNT(*) FROM projects
UNION ALL
SELECT 'repositories', COUNT(*) FROM repositories;

-- 2. Verify foreign key relationships
SELECT
  o.name AS organization,
  p.name AS project,
  r.name AS repository,
  r.git_url
FROM organizations o
JOIN projects p ON o.id = p.organization_id
JOIN repositories r ON p.id = r.project_id
ORDER BY o.name, p.name, r.name;

-- 3. Verify timestamps are populated
SELECT
  'organizations' AS table_name,
  COUNT(*) FILTER (WHERE created_at IS NOT NULL) AS created_at_count,
  COUNT(*) FILTER (WHERE updated_at IS NOT NULL) AS updated_at_count
FROM organizations
UNION ALL
SELECT
  'projects',
  COUNT(*) FILTER (WHERE created_at IS NOT NULL),
  COUNT(*) FILTER (WHERE updated_at IS NOT NULL)
FROM projects
UNION ALL
SELECT
  'repositories',
  COUNT(*) FILTER (WHERE created_at IS NOT NULL),
  COUNT(*) FILTER (WHERE updated_at IS NOT NULL)
FROM repositories;

-- 4. Test unique constraints (should fail if uncommented)
-- INSERT INTO organizations (name, slug) VALUES ('Test Org', 'acme-corp');

-- 5. Test multi-tenant scoping - projects per organization
SELECT
  o.name AS organization,
  COUNT(p.id) AS project_count
FROM organizations o
LEFT JOIN projects p ON o.id = p.organization_id
GROUP BY o.name
ORDER BY o.name;

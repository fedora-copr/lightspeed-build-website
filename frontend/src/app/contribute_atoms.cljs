(ns app.contribute-atoms
  (:require [reagent.core :as r]))


(def how-to-fix (r/atom nil))
(def fail-reason (r/atom nil))
(def snippets (r/atom []))
(def files (r/atom nil))

(def error-description (r/atom nil))
(def error-title (r/atom nil))
(def submitted (r/atom nil))
(def backend-data (r/atom nil))
(def log (r/atom nil))
(def fas (r/atom nil))
(def build-id (r/atom nil))
(def build-id-title (r/atom nil))
(def build-url (r/atom nil))

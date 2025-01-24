(ns app.homepage
  (:require [reagent.core :as r]
            [cljs.math :as math]
            [clojure.string :as str]
            [cljs.core.match :refer-macros [match]]
            [app.homepage-validation :refer [validate]]
            [lambdaisland.fetch :as fetch]
            [app.components.jumbotron :refer [render-error]]
            [app.helpers :refer
             [current-path
              local-storage-enabled
              local-storage-error
              remove-trailing-slash]]))

(def current-hash (r/atom (. (. js/document -location) -hash)))

(def input-values (r/atom nil))
(def input-errors (r/atom []))
(def backend-stats (r/atom nil))
(def current-hash-atom (r/atom (or (and (not (str/blank? @current-hash)) @current-hash) "#copr")))
(def report-target (r/atom 1000))
(def error (r/atom nil))

(defn fetch-stats-backend []
  (let [url (remove-trailing-slash (str "/stats" (current-path)))]
    (-> (fetch/get url {:accept :json :content-type :json})
        (.then (fn [resp]
                 (-> resp :body (js->clj :keywordize-keys true))))
        (.then (fn [resp]
                 (reset! backend-stats resp))))))

(defn on-submit [event]
  (.preventDefault event)
  (validate current-hash-atom input-values input-errors)
  (let [source (str/replace @current-hash-atom "#" "")
        params (match @current-hash-atom
                 "#copr"      [(get @input-values :copr-build-id)
                               (get @input-values :copr-chroot)]
                 "#packit"    [(get @input-values :packit-id)]
                 "#koji"      [(get @input-values :koji-build-id)
                               (get @input-values :koji-arch)]
                 "#url"       [(js/btoa (get @input-values :url))]
                 "#container" [(js/btoa (get @input-values :url))])
        url (str/join "/" (concat ["/contribute" source] (map str/trim params)))]
    (when (empty? @input-errors)
      (set! (.-href (.-location js/window)) url))))

(defn on-submit-upload [event]
  (.preventDefault event)
  (if-not (local-storage-enabled)
    (reset! error (local-storage-error))
    (do
      (.setItem js/localStorage "name" (get @input-values :name))
      (.setItem js/localStorage "content" (get @input-values :file))
      (validate current-hash-atom input-values input-errors)
      (when (empty? @input-errors)
        (set! (.-href (.-location js/window)) "/contribute/upload")))))

(defn on-tab-click [href]
  (reset! current-hash-atom href)
  (reset! input-errors []))

(defn on-input-change [target]
  (swap! input-values assoc
         (keyword (.-name target))
         (.-value target)))

(defn on-input-change-file [event]
  (let [target (.-target event)
        path (-> target .-value)
        name (last (str/split path #"\\"))
        reader (js/FileReader.)
        set-atom (fn [e]
                   (swap! input-values assoc :name name)
                   (swap! input-values assoc
                          (keyword (.-name target))
                          (-> e .-target .-result)))
        file (first (array-seq (.. event -target -files)))]
    (.addEventListener reader "load" set-atom)
    (.readAsText reader file)))

(defn render-navigation-item [title href]
  (let [active? (= href @current-hash-atom)]
    [:li {:class "nav-item ml-auto"}
     [:a {:class ["nav-link" (if active? "active" nil)]
          :href href
          :on-click #(on-tab-click href)}
      title]]))

(defn render-navigation []
  [:div {:class "card-header"}
   [:ul {:class "nav nav-tabs card-header-tabs"}
    (render-navigation-item "Copr" "#copr")
    (render-navigation-item "Koji" "#koji")
    (render-navigation-item "Packit" "#packit")
    (render-navigation-item "URL" "#url")
    (render-navigation-item "Upload" "#upload")
    (render-navigation-item "Container" "#container")]])

(defn progress-width []
  (min
   (math/ceil
    (*
     (float
      (/
       (:total_reports @backend-stats)
       @report-target)) 100)) 100))

(defn render-stats []
  [:<>
   [:div {:id "progressbar"}
    [:p {:id "progressbar-number"} (str (progress-width) "%")]
    [:div {:id "progress"
           :style {:width (str (progress-width) "%")}}]
    [:div {:id "progressbar2"}
     [:div
      [:p "Collected " (:total_reports @backend-stats) " logs from "
       [:a {:href "/documentation#goals"} @report-target]]]]]])

(defn render-card [provider url title img text inputs]
  [:div {:class "card-body"}
   [:div {:class "row"}
    [:div {:class "col-2"}
     [:a {:href url :title (if provider (str "Go to " provider) nil)}
      [:img {:src img, :class "card-img-top", :alt "..."}]]]

    [:div {:class "col-8"}
     [:h2 {:class "card-title"} title]
     [:p {:class "card-text"} text]

     [:form
      (if (= provider "Upload")
        {:action "/contribute/upload"
         :method "POST"
         :on-submit #'on-submit-upload
         :enc-type "multipart/form-data"}
        {:on-submit #'on-submit})
      (for [[i input] (map-indexed vector inputs)]
        ^{:key i}
        [:div {:class "input-group mb-3 container"}
         input
         (when (= (- (count inputs) 1) i)
           [:button
            {:class "btn btn-outline-primary",
             :type "submit"}
            "Let's go"])])]]

    [:div {:class "col-2"}]]])

(defn input [name placeholder]
  [:input
   {:type "text"
    :class ["form-control"
            (when (some #{name} @input-errors)
              "validation-error")]
    :name name
    :placeholder placeholder
    :value (get @input-values (keyword name))
    :on-change #(on-input-change (.-target %))}])

(defn input-file [name]
  [:input
   {:type "file"
    :class ["form-control"
            (when (some #{name} @input-errors)
              "validation-error")]
    :name name
    :on-change #(on-input-change-file %)}])

(defn render-copr-card []
  (render-card
   "Copr"
   "https://copr.fedorainfracloud.org"
   "Submit logs from Copr"
   "img/copr-logo.png"
   "Specify a Copr build ID and we will fetch and display all relevant logs."
   [(input "copr-build-id" "Copr build ID, e.g. 6302362")
    (input "copr-chroot" "Chroot name, e.g. fedora-39-x86_64 or srpm-builds")]))

(defn render-packit-card []
  (render-card
   "Packit"
   "https://dashboard.packit.dev"
   "Submit logs from Packit"
   "img/packit-logo.png"
   (str/join "" ["Specify a Packit job ID, we will match it to a build, "
                 "fetch, and display all relevant logs."])
   [(input "packit-id" "Packit job ID, e.g. 1015788")]))

(defn render-koji-card []
  (render-card
   "Koji"
   "https://koji.fedoraproject.org"
   "Submit logs from Koji"
   "img/koji-logo.png"
   "Specify a Koji build ID or task ID with method=buildArch to fetch and display all relevant logs."
   [(input "koji-build-id" "Koji build ID, e.g. 2274591")
    (input "koji-arch" "Architecture, e.g. x86_64")]))

(defn render-url-card []
  (render-card
   nil
   nil
   "Submit RPM logs from URL"
   "img/url-icon.png"
   (str/join "" ["Paste an URL to a log file, or a build in some build system. "
                 "If recognized, we will fetch and display all relevant logs."])
   [(input "url" "https://paste.centos.org/view/raw/5ba21754")]))

(defn render-upload-card []
  (render-card
   "Upload"
   nil
   "Upload RPM logs from your computer"
   "img/upload-icon.png"
   "Upload a RPM log file from your computer. "
   [(input-file "file")]))

(defn render-container-card []
  (render-card
   nil
   nil
   "Submit container logs from URL"
   "img/url-icon.png"
   "Paste an URL to a raw container log file."
   [(input "url" "https://paste.centos.org/view/raw/5ba21754")]))

(defn render-cards []
  (match @current-hash-atom
    "#copr"      (render-copr-card)
    "#packit"    (render-packit-card)
    "#koji"      (render-koji-card)
    "#url"       (render-url-card)
    "#upload"    (render-upload-card)
    "#container" (render-container-card)
    :else        (render-copr-card)))

(defn homepage []
  (if @error
    (render-error (:title @error) (:description @error))
    [:div {:class "card text-center"}
     (render-stats)
     (render-navigation)
     (render-cards)])
  [:div {:class "card text-center"}
   (render-stats)
   (render-navigation)
   (render-cards)])

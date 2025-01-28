(ns app.prompt.core
  (:require
   [reagent.core :as r]
   [malli.core :as m]
   [clojure.string :as str]
   [ajax.core :refer [POST]]
   [app.components.jumbotron :refer
    [render-error
     loading-screen]]))

;; Copy-pasted from review
(def error-description (r/atom nil))
(def error-title (r/atom nil))
(def status (r/atom nil))

;; Copy-pasted from review
(defn handle-backend-error [title description]
  (reset! status "error")
  (reset! error-title title)
  (reset! error-description description))

;; Copy-pasted from review
(defn handle-validation-error [title description]
  ;; Go back to "has files" state, let users fix
  ;; validation errors
  (reset! status nil)
  (reset! error-title title)
  (reset! error-description description))

(def form (r/atom nil))
(def prompt-value (r/atom nil))

(def InputSchema
  [:map {:closed true}
   [:explanation :string]
   [:certainty :int]

   [:reasoning
    [:vector
     [:map
      [:snippet :string]
      [:comment :string]]]]

   [:log
    [:map
     [:name :string]
     [:content :string]]]])

;; TODO We allow nil for easier debugging now but should reject it later
(def OutputSchema
  [:map {:closed true}
   [:prompt [:maybe :string]]])

(defn send []
  (let [query-url (-> js/window .-location .-search js/URLSearchParams. (.get "url"))
        data (if query-url
               {:prompt query-url}
               {:prompt @prompt-value})]
    (if (m/validate OutputSchema data)
      (do
        (reset! status "waiting")
        (POST "/frontend/explain/"
          :params data
          :format :json
          :response-format :json
          :keywords? true

          :error-handler
          (fn [error]
            (handle-backend-error
             (:error (:response error))
             (:description (:response error))))

          :handler
          (fn [data]
            (if (m/validate InputSchema data)
              (do
                (reset! status "ok")
                (reset! form data))
              (handle-backend-error
               "Invalid data"
               "Got invalid data from the backend. This is likely a bug.")))))

      (if (not query-url)
        (reset! status "No URL provided. Please enter a URL.")
        (handle-backend-error
         "Client error"
         "Something went wrong when preparing a request to server")))))

(defn on-change-prompt [event]
  (let [target (.-target event)
        value (.-value target)]
    (reset! prompt-value value)))

(defn prompt-form []
  [:<>
   [:label {:for "promptTextarea"}]
   [:form
    [:div
     {:class "input-group text-center"}
     ;; [:span
     ;;  {:class "input-group-addon btn btn-secondary"}
     ;;  [:i {:class "fa-solid fa-upload prompt-icon"}]]
     [:textarea
      {:class "form-control"
       :id "promptTextarea"
       :rows "3",
       :placeholder
       (str "Paste a link to your failed RPM build log.")
       :on-change on-change-prompt}]
     [:span
      {:class "input-group-addon btn btn-primary"
       :on-click send}
      [:i {:class "fa-solid fa-play prompt-icon"}]]]]])

(defn certainty-icon [percent]
  (let [title (str "The AI is " percent "% certain of this response")]
    (cond
      (> percent 90)
      [:i {:class "fa-regular fa-face-smile text-primary" :title title}]

      (> percent 70)
      [:i {:class "fa-regular fa-face-meh text-warning" :title title}]

      :else
      [:i {:class "fa-regular fa-face-frown-open text-danger" :title title}])))

(defn left-column []
  [:div {:class "col-6", :id "left-column"}
   [:h2 {:class "float-end"}
    (certainty-icon (:certainty @form))]
   [:h2 "Explanation"]
   (map (fn [x] [:p x])
        (-> @form :explanation (str/split #"\n")))
   [:div
    {:class "container", :id "prompt"}
    (prompt-form)]])

(defn reason [id snippet comment]
  (let [accordion-id "#accordionExample"
        heading-id (str "heading-reason-" id)
        collapse-id (str "collapse-reason-" id)]
    [:div {:class "accordion-item" :key id}
     [:h2 {:class "accordion-header" :id heading-id}
      [:button
       {:class "accordion-button collapsed"
        :type "button",
        :data-bs-toggle "collapse"
        :data-bs-target (str "#" collapse-id)
        :aria-expanded "false"
        :aria-controls collapse-id}
       [:code {:class "text-truncate"} snippet]]]

     [:div
      {:id collapse-id
       :class "accordion-collapse collapse"
       :aria-labelledby heading-id
       :data-bs-parent accordion-id}
      [:div
       {:class "accordion-body"} comment]]]))

(defn download []
  (let [name (-> @form :log :name)
        content (-> @form :log :content)
        mime "text/plain"

        a (.createElement js/document "a")
        blob (new js/Blob #js [content] #js {:type mime})
        url (.createObjectURL js/URL blob)]
    (.setAttribute a "href" url)
    (.setAttribute a "download" name)
    (.click a)))

(defn right-column []
  [:div {:class "col-6", :id "right-column"}
   [:button {:type "button"
             :class "btn btn-outline-primary float-end"
             :on-click download}
    [:i {:class "fa-solid fa-floppy-disk"}]
    " Full log"]

   [:h2 "Reasoning"]
   [:div {:class "accordion accordion-flush" :id "accordionExample"}
    (map-indexed
     (fn [i x] (reason i (:snippet x) (:comment x)))
     (:reasoning @form))]])

(defn two-column-layout []
  [:div {:class "row" :id "content"}
   (left-column)
   (right-column)])

(defn card [title body]
  [:div {:class "card"}
   [:div {:class "card-body"}
    [:h5 {:class "card-title"} title]
    body]])

(defn on-click-example [event]
  (let [target (.-target event)
        value (.-prompt (.-dataset target))]
    (reset! prompt-value value)
    (send)))

(defn disclaimer []
  [:div {:class "alert alert-warning text-left" :role "alert"}
   [:p (str "Welcome to the new experimental functionality of Log Detective, thank you for your interest!")]
   [:p (str "This is our initial prototype that will 'Explain' a log of your choice to you. It has several limitations:")]
   [:ol
    [:li (str "The inference is slow and serial. We can only process a single request in the background. It will take at least 30 seconds to give you a response. In case of multiple requests, it can ramp up to minutes.")]
    [:li (str "We use a general-purpose mistral model in the background. The collected data are not being used here just yet. We are still working on fine-tuning our own model.")]
    [:li (str "Please report any issues you'll encounter. We don't have any alerting in place, the deployment is highly experimental.")]]])

(defn prompt-only []
  [:div {:id "content-narrow" :class "container"}
   [:section
    {:class "py-1 text-center container"}
    [:div
     {:class "row py-lg-3"}
     [:div
      {:class "col-md-10 mx-auto"}
      [:h1 {:class "fw-light"} "Log Detective"]
      [:p
       {:class "lead text-body-secondary"}
       (str "Trying to improve RPM packaging experience by analyzing build "
            "logs and explaining the failure in simple words.")]
      (disclaimer)

      [:div {:class "py-4"}
       (prompt-form)]]]]

      ;;[:div {:id "prompt-examples"}
      ;; [:button {:type "button"
      ;;           :class "btn btn-outline-secondary"
      ;;           :on-click on-click-example
      ;;           :data-prompt "TODO Example 1 prompt"}
      ;;  "Example 1"]

      ;; [:button {:type "button"
      ;;           :class "btn btn-outline-secondary"
      ;;           :on-click on-click-example
      ;;           :data-prompt "TODO Example 2 prompt"}
      ;;  "Example 2"]

      ;; [:button {:type "button"
      ;;           :class "btn btn-outline-secondary"
      ;;           :on-click on-click-example
      ;;           :data-prompt "TODO Example 3 prompt"}
      ;;  "Example 3"]

      ;; [:button {:type "button"
      ;;           :class "btn btn-outline-secondary"
      ;;           :on-click on-click-example
      ;;           :data-prompt "TODO Example 4 prompt"}
      ;;  "Example 4"]]]]]

   [:div {:class "container" :id "about"}
    [:h2 {:class "text-center"} "About the project"]

    (card "Debugging failed builds is hard"
          (str "Each build produces thousands of lines of output split among "
               "multiple log files. And the relevant error message can be "
               "anywhere. It's just like a needle in a haystack."))

    (card "Does it matter?"
          (str "Veteran packagers have an intuition where the error message "
               "will most likely be, but the process is tedious regardless. "
               "Newbies are often overwhelmed by the complexity and miss the "
               "error message completely."))

    (card "What is our goal?"
          (str "Training an AI model to understand RPM build logs and explain "
               "the failure in simple words, with recommendations how to fix "
               "it. You won't need to open the logs at all."))]])

(defn prompt []
  (let [query-url (-> js/window .-location .-search js/URLSearchParams. (.get "url"))]
    (if query-url
      (do
        (send)
        (loading-screen "Getting a response from the server"))
      (cond
        (= @status "error")
        (render-error @error-title @error-description)

        ;; TODO If we are already in a two-column-layout, we should print only
        ;; a small loading icon somewhere, not a jumbotron
        (= @status "waiting")
        (loading-screen "Getting a response from the server")

        @form
        (two-column-layout)

        :else
        (prompt-only)))))
